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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from kobo_api import delivery
from kobo_api.catalog import _parse_persona_file
from kobo_api.clients import ClientError, StorageClient, StorageNotFound
from kobo_api.purpose_tags import normalize_tags
from kobo_api.search import CatalogEntry

# A persona id: lowercase, starts with a letter, up to 40 chars of
# letters/digits/hyphens after that (trailing hyphens are fine — see
# persona_id's docstring for why that can legitimately happen after
# truncation; contrast with kobo_api.catalog.NAME_RE, which bans them).
PERSONA_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")

_TAG_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")

_NON_ALNUM_RUN_RE = re.compile(r"[^a-z0-9]+")

# Short-names ruling (2026-09-24): a persona name is one or two words,
# three at the very most. "code-grounded-player-intel" and
# "direct-factual-condensed-mellow" read as a trait list, not a name; the
# who_they_are line is where the traits live. Enforced at create (room UI,
# MCP tool, and the drafter's own output all land here), never on read, so
# older long names in an existing index keep working.
PERSONA_NAME_MAX_WORDS = 3
_NAME_WORD_RE = re.compile(r"[a-z0-9]+")

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
    ["a", "an", "the", "and", "or", "but", "nor", "of", "to", "in", "on", "at", "for", "with", "without", "by", "from", "as", "is", "are", "was", "were", "be", "been", "being", "who", "whom", "whose", "what", "which", "when", "where", "why", "how", "they", "them", "their", "theirs", "you", "your", "yours", "we", "us", "our", "ours", "i", "me", "my", "mine", "he", "him", "his", "she", "her", "hers", "it", "its", "this", "that", "these", "those", "itself", "himself", "herself", "myself", "yourself", "do", "does", "did", "done", "doing", "have", "has", "had", "having", "will", "would", "shall", "should", "could", "can", "may", "might", "must", "not", "no", "so", "if", "then", "than", "each", "every", "any", "all", "some", "one", "two", "three", "first", "second", "next", "last", "more", "most", "other", "another", "such", "only", "own", "same", "too", "very", "s", "t", "re", "ve", "ll", "d", "m", "just", "also"]
)

_WORD_RE = re.compile(r"[a-z0-9]+")


class PersonaError(Exception):
    """A persona field is invalid in a way pydantic alone can't catch:
    currently just `persona_id` failing to derive a valid id from the
    submitted name (e.g. it slugifies to empty, or to something that
    starts with a digit)."""


class PersonaExists(Exception):
    """A persona with this id is already in the index. `entry` is that
    index entry, so the route can say where the existing one is filed
    (2026-11-17: a person who could not see their persona in the list tried
    to make it again and was told only that the name was taken)."""

    def __init__(self, message: str, entry: dict | None = None) -> None:
        super().__init__(message)
        self.entry = entry or {}


# --- the house shape (2026-11-17) -------------------------------------------
#
# Every persona the room writes has the same three parts, in the same order:
# the opening line, `## What you do`, `## Rules`. The built-ins are written
# that way by hand; a team's own persona used to be whatever was typed or
# pasted, wrapped in the skeleton as it came. Three things went wrong in the
# catalog because of it: a pasted document brought its own title and
# sections, so the persona had two openings and headings above the house's
# own; a who-they-are line that ended in a period rendered with two; and a
# persona could be one thin sentence with no rules at all. The cleaners
# below run on every PersonaFields, so the form, the drafter and the MCP
# tool all land in the same shape, and `check_substance` holds the floor.

# What a hand-typed who-they-are tends to start with. The opening line
# already says "You are {name}, ", so the field carries only what follows.
_WHO_PREFIXES = ("you are {name},", "you are {name}", "you are ")

# The fewest characters of what-they-do, and the fewest rules, a new or
# edited persona may carry. The built-ins run to about 1,700 characters and
# ten to twelve rules; these are floors, not targets. The rules floor holds
# only in a room whose agents still keep their rules in their persona: with
# guardrails on, rules belong in each agent's own Guardrails, so a persona
# people write needs none (2026-11-20).
PERSONA_WHAT_MIN_CHARS = 300
PERSONA_RULES_MIN = 3

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_TOP_HEADING_RE = re.compile(r"^#{1,2}[ \t]+(?=\S)")
_BULLET_RE = re.compile(r"^(?:[-*\u2022]|\d+[.)])[ \t]+")
_BARE_MARKER_RE = re.compile(r"^(?:[-*\u2022]|\d+[.)])$")


def clean_who(text: str) -> str:
    """The who-they-are line as the opening line wants it: what follows
    "You are {name}, ", with no period of its own. Strips a leading "You
    are" somebody typed out of habit and any closing period, so the
    rendered line never reads "You are {name}, You are a ..." or ends in
    two periods. Idempotent."""
    value = text.strip()
    while True:
        lowered = value.lower()
        for prefix in _WHO_PREFIXES:
            if lowered.startswith(prefix):
                value = value[len(prefix):].strip()
                break
        else:
            break
    return value.rstrip(". ").strip()


def house_body(text: str) -> str:
    """The what-they-do text with nothing in it that outranks the house's
    own sections: a `#` or `##` heading becomes `###`, outside a code
    fence. A persona has exactly two sections of that rank, and
    `parse_persona_body` finds them by their headings, so a pasted "## Rules"
    in the middle of the body used to cut the persona in two when it was
    read back. Blank lines at either end go. Idempotent."""
    out: list[str] = []
    fenced = False
    for line in text.strip("\n").splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
        elif not fenced:
            line = _TOP_HEADING_RE.sub("### ", line)
        out.append(line.rstrip())
    return "\n".join(out).strip("\n")


def clean_rule(text: str) -> str:
    """One rule as one bullet's text: the list marker somebody pasted along
    with it ("- ", "* ", "1. ") goes, since the renderer writes its own.
    Every marker goes, not just the first, and a rule that was only a
    marker comes back empty, so cleaning a cleaned rule changes nothing."""
    value = text.strip()
    while True:
        stripped = _BULLET_RE.sub("", value).strip()
        if stripped == value:
            break
        value = stripped
    return "" if _BARE_MARKER_RE.match(value) else value


def _one_line(text: str) -> bool:
    """True when `text` is one line the way `parse_persona_body` reads
    lines (`str.splitlines`), which breaks on a carriage return and the
    Unicode line separators as well as on a newline."""
    return len(text.strip().splitlines()) <= 1


class PersonaFields(BaseModel):
    """The Persona builder v2 form (addendum): four fields, down from
    seven. Role/Purpose/Tone/Tone-note/Boundaries/Tags are gone — Rules
    absorbs Boundaries' job (a short list of always/never one-liners).

    purpose_tags returned 2026-08-28 (create-flow polish), DELIBERATE this
    time: noun-derivation (`_derive_tags`, retired) produced tags like
    "embarrassment" at scale, so tags now come only from the model's draft
    or the human's hand — normalized and capped at create, empty when
    neither supplied. An empty tag list is honest; a derived one lied."""

    name: str = Field(min_length=1, max_length=60)
    who_they_are: str = Field(min_length=1, max_length=200)
    # what_they_do's own ceiling (upload/limits addendum) is generous on
    # purpose: 49152 chars is a sane absolute wall, not the real gate. The
    # real gate is the MERGED persona.md's own 200-line/49152-byte cap,
    # checked in create_persona against the actual rendered body (see
    # _check_rendered_size) — this Field bound just keeps a single field
    # from being unbounded in isolation.
    what_they_do: str = Field(min_length=1, max_length=49152)
    # Create-flow polish (2026-08-28): supplied by the draft flow or the
    # human, never derived. Normalized/capped in create_persona.
    purpose_tags: list[str] = Field(default_factory=list, max_length=10)
    # 0-150 optional one-liners ("things they always or never do") — Rules
    # is explicitly optional in the brief, unlike who_they_are/what_they_do.
    # Same story as what_they_do above: 150 items is a sane wall, not the
    # real gate (that's the merged render's line/byte cap in create_persona —
    # see comment below, where the real numbers live).
    rules: list[str] = Field(default_factory=list, max_length=150)

    @field_validator("who_they_are")
    @classmethod
    def _who_they_are_one_line(cls, v: str) -> str:
        if not _one_line(v):
            raise ValueError("who_they_are must be a single line (no newlines)")
        cleaned = clean_who(v)
        if not cleaned:
            raise ValueError("who_they_are must say who the persona is")
        return cleaned

    @field_validator("what_they_do")
    @classmethod
    def _what_they_do_house_shape(cls, v: str) -> str:
        cleaned = house_body(v)
        if not cleaned:
            raise ValueError("what_they_do must not be blank")
        return cleaned

    @field_validator("rules")
    @classmethod
    def _rules_shape(cls, v: list[str]) -> list[str]:
        """Each rule must be non-empty and a single line (one bullet, one
        line of the rendered body) — no numeric per-rule length cap any
        more (upload/limits addendum dropped the old 200-char ceiling): a
        long rule is bounded by the merged render's own 200-line/49152-byte
        gate (create_persona), not a per-field lookalike of it."""
        cleaned: list[str] = []
        for item in v:
            if not _one_line(item):
                raise ValueError(f"each rule must be a single line (no newlines): {item!r}")
            rule = clean_rule(item)
            if not rule:
                raise ValueError(f"each rule must not be empty: {item!r}")
            cleaned.append(rule)
        return cleaned


# The order the built-in personas are shown in, everywhere the room lists
# them (the Personas page, the Create-agent picker, the MCP list). Curated,
# not alphabetical, and read top to bottom by the people it is for: the
# everyday personas a non-technical newcomer would pick sit first, the
# specialist and engineering ones follow, and the operators of specific
# clouds close the list (operator ruling 2026-09-24: "showing the
# non-technical ones first is less daunting"). A new template must be
# placed here, once; `tests/test_persona_templates.py` refuses one that is
# not. Team personas are never in this list and always follow it, by name.
BUILTIN_ORDER: tuple[str, ...] = (
    "helpful-generalist",
    "billing-analyst",
    "identity-admin",
    "code-reviewer",
    "gcp-operator",
)

# Packaged templates that are a GROUP's personas rather than the room's
# built-ins (2026-09-29: five built-ins, the rest filed where they belong).
# The seeder writes each under its group, not the commons, and the create
# form shows them behind that group's pill. Keys are template ids, values
# are group names from the org config.
TEAM_TEMPLATES: dict[str, str] = {
    "aws-operator": "infra-engineering",
    "azure-operator": "infra-engineering",
    "frontend-designer": "navex-ios",
    "legal-reader": "security",
    "marketing-strategist": "marketing",
    "workspace-admin": "corporate-it",
}

# Templates the room no longer ships. An index entry left over from an
# earlier seed is archived on the next boot, never resurrected, and never
# deleted: agents built from it keep running.
RETIRED_TEMPLATES: tuple[str, ...] = ("onboarding-guide",)
_BUILTIN_RANK = {id_: rank for rank, id_ in enumerate(BUILTIN_ORDER)}


def in_reading_order(entries: list[dict]) -> list[dict]:
    """The catalog entries as a person should read them: built-ins first, in
    `BUILTIN_ORDER` (a built-in the list does not name yet goes after the
    named ones, by name), then every team persona by name.

    The search layer already puts built-ins ahead of team personas; it can
    only sort by columns it stores, and the reading order is not a column,
    so it is applied here, once, for every caller that lists personas. Same
    input order in, same output order out: the sort is stable."""
    unlisted = len(BUILTIN_ORDER)

    def key(entry: dict) -> tuple[int, int, str]:
        if entry.get("builtin"):
            return (0, _BUILTIN_RANK.get(entry.get("id", ""), unlisted), entry.get("name") or "")
        return (1, 0, entry.get("name") or "")

    return sorted(entries, key=key)


def write_persona_file(
    github, repo: str, body: str, *, persona_id: str, message: str | None = None
) -> str:
    """Make `bundle/persona.md` in `repo` say `body`, and push it.

    The one file the runtime reads its persona from, rendered by the
    caller exactly as the pipeline renders it at birth (`{name}` already
    substituted, the catalog's header line already gone), so a changed
    persona and a born-with one are byte-for-byte the same kind of file.
    Same shape as `kobo_api.knowledge.save_knowledge_file`: clone, write,
    `commit_push`, return the pushed head. Identical content is a
    `commit_push` that reports nothing to do, and the returned head is then
    the truthful answer: the bundle already says this.

    Never `[skip ci]`: the persona reaches the running agent only through
    the rebuild and rollout the generated CI does on every push, which is
    the delivery, not a side effect of it. `message` names an edit in place
    (the agent keeps its persona); without one the commit says which
    persona the agent wears now.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        bundle = workdir / "bundle"
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "persona.md").write_text(body, encoding="utf-8")
        delivery.push(github, workdir, message or f"kobo: persona is now {persona_id}")
        return github.local_head(workdir)


def check_name_length(name: str) -> None:
    """Raise `PersonaError` when `name` has more than
    `PERSONA_NAME_MAX_WORDS` words (words = alphanumeric runs, so
    "Ada (Release)" is two and "code-grounded-player-intel" is four)."""
    words = _NAME_WORD_RE.findall(name.lower())
    if len(words) > PERSONA_NAME_MAX_WORDS:
        raise PersonaError(
            f"persona names are one or two words, three at most: {name!r} has "
            f"{len(words)}. Put the traits in who_they_are, not the name"
        )


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


# --- reading a rendered persona back apart ---------------------------------

# The three fixed landmarks render_persona lays down, written once here so
# the parser below can never drift from the renderer by a stray character.
_WHO_LINE_RE = re.compile(r"^You are \{name\}, (?P<who>.+)\.$")
_WHAT_HEADING = "## What you do"
_RULES_HEADING = "## Rules"
_DESCRIPTION_LINE_RE = re.compile(r"^<!--\s*description:.*-->\s*$")


@dataclass
class ParsedPersona:
    """What `parse_persona_body` could recover from a persona.md.

    `who_they_are`/`what_they_do` are None (not "") when the landmark that
    carries them isn't in the file at all — the honest answer for a
    packaged built-in, whose body is plain prose with no sections. The
    caller decides what to show instead; the parser never guesses.
    """

    who_they_are: str | None = None
    what_they_do: str | None = None
    rules: list[str] = field(default_factory=list)


def _rules_heading_at(lines: list[str], start: int) -> int:
    """The index of the house's own `## Rules` heading at or after `start`,
    or `len(lines)` when the body has none.

    The LAST such line, not the first: `render_persona` writes the heading
    after the whole of what-they-do and follows it with bullets only, so
    nothing below it can be another. The first one cut a persona in two
    when its body quoted the heading inside a code fence (the cleaners
    leave a fence alone), and an edit then saved the cut for good
    (2026-11-17 review). A persona with no rules has no house heading, so
    a last match that sits inside a closed fence is the body's own text.
    """
    at = next(
        (i for i in range(len(lines) - 1, start - 1, -1) if lines[i] == _RULES_HEADING),
        None,
    )
    if at is None:
        return len(lines)
    fences_before = sum(1 for line in lines[start:at] if _FENCE_RE.match(line))
    closed_after = any(_FENCE_RE.match(line) for line in lines[at + 1 :])
    if fences_before % 2 == 1 and closed_after:
        return len(lines)
    return at


def parse_persona_body(text: str) -> ParsedPersona:
    """The inverse of `render_persona`: read a stored persona.md back into
    the three fields that wrote it.

    Round-trip is the contract (test_personas.py pins it both ways): every
    body `render_persona` emits parses back field for field, braces and
    blank lines included. Nothing here calls `str.format` or any markdown
    renderer — the `{name}` placeholder in the opening line is matched
    literally, and free-form persona text that carries its own `{`/`}`
    passes through untouched.

    A leading `<!-- description: ... -->` header (the packaged templates'
    convention, see `kobo_api.catalog._parse_persona_file`) is skipped
    first. A body with no `## What you do` heading — every packaged
    built-in — yields None for both prose fields and no rules, rather than
    a guess at which paragraph was meant to be which.
    """
    lines = text.splitlines()
    if lines and _DESCRIPTION_LINE_RE.match(lines[0]):
        lines = lines[1:]

    parsed = ParsedPersona()

    # The `## What you do` heading is the tell that render_persona wrote
    # this file. Without it nothing else is read either -- not even the
    # opening line, which in a packaged template is ordinary prose that
    # merely starts the same way ("You are {name}, a helpful assistant. Be
    # concise...") and would otherwise be mistaken for a who-they-are.
    try:
        what_at = lines.index(_WHAT_HEADING)
    except ValueError:
        return parsed

    for line in lines[:what_at]:
        match = _WHO_LINE_RE.match(line)
        if match:
            parsed.who_they_are = match.group("who")
            break

    rules_at = _rules_heading_at(lines, what_at + 1)

    body = lines[what_at + 1 : rules_at]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    parsed.what_they_do = "\n".join(body)

    parsed.rules = [
        line[2:].strip() for line in lines[rules_at + 1 :] if line.startswith("- ")
    ]
    return parsed


def split_rules(body: str) -> tuple[str, list[str]]:
    """`body` without its house `## Rules` section, and the rules that section held
    (issue #524: at create they start the agent's guardrails, so a rule lives in one
    place). A body without the house headings comes back whole with no rules: a
    persona the room didn't write in its shape keeps its rules where its author put
    them, the same "never guess" `parse_persona_body` keeps."""
    lines = body.splitlines()
    try:
        what_at = lines.index(_WHAT_HEADING)
    except ValueError:
        return body, []
    rules_at = _rules_heading_at(lines, what_at + 1)
    if rules_at == len(lines):
        return body, []
    rules = [line[2:].strip() for line in lines[rules_at + 1 :] if line.startswith("- ")]
    kept = lines[:rules_at]
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept) + "\n", [rule for rule in rules if rule]


# --- one agent's own persona (2026-11-20) -----------------------------------
#
# An agent copies its persona when it is made: its `bundle/persona.md` is the
# catalog text with the agent's own name where `{name}` was. Its owners edit that
# copy in the room (the Persona section, `PUT /api/agents/{name}/persona/text`),
# so changing one agent's persona no longer means editing the file on GitHub. The
# catalog persona stays as it was unless they also save it there.


def parse_agent_persona(text: str, agent_name: str) -> ParsedPersona:
    """`parse_persona_body` for an agent's own persona.md, whose opening line names
    the agent where the catalog text says `{name}`: the name is put back first, so
    the same landmarks find the same fields."""
    lines = text.splitlines()
    named = f"You are {agent_name}, "
    for index, line in enumerate(lines):
        if line == _WHAT_HEADING:
            break
        if line.startswith(named):
            lines[index] = "You are {name}, " + line[len(named):]
            break
    return parse_persona_body("\n".join(lines))


def render_agent_persona(
    agent_name: str, who_they_are: str, what_they_do: str, rules: list[str]
) -> str:
    """An agent's own persona.md in the house shape, byte for byte the way the room
    writes it at birth: `render_persona` with the agent's name in place of `{name}`.
    The same cleaners and the same floor on what-they-do as a persona edit, and no
    floor on rules: they are this agent's, and with guardrails on they live in its
    Guardrails. Raises PersonaError, or pydantic.ValidationError from the fields."""
    fields = PersonaFields(
        name=agent_name[:60],
        who_they_are=who_they_are,
        what_they_do=what_they_do,
        rules=rules,
    )
    check_substance(fields, rules_required=False)
    body = render_persona(fields).replace("{name}", agent_name)
    _check_rendered_size(body)
    return body


def clean_persona_text(text: str) -> str:
    """A persona.md the room did not write in its shape, saved as one text: each
    line's trailing spaces and the blank lines at either end go, and it ends in one
    newline. Raises PersonaError when nothing is left or it is over the size limit."""
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        raise PersonaError("the persona can't be empty")
    body = "\n".join(lines) + "\n"
    _check_rendered_size(body)
    return body


# The room's real limit on a persona (upload/limits addendum): the MERGED
# persona.md — render_persona's output, body plus rules section together —
# tops out at 200 lines and 49152 bytes (48KB, measured on the UTF-8
# encoding). Enforced in create_persona against the actual rendered body,
# never against a per-field lookalike (PersonaFields' own what_they_do/
# rules bounds above are generous walls, not this real gate).
# Tag cap at create (create-flow polish, 2026-08-28): supplied tags only,
# normalized, at most this many kept — mirrors the create route's own cap
# posture rather than trusting the wire shape.
_TAG_CAP = 8

_MAX_RENDERED_LINES = 200
_MAX_RENDERED_BYTES = 49152


def _check_rendered_size(body: str) -> None:
    """Raise PersonaError when `body` (render_persona's output) exceeds the
    room's real persona.md ceiling — checked against the actual rendered
    bytes, so the message always names the real numbers rather than
    reasoning about any one field in isolation. Line count first, then byte
    count: either violation is reported on its own (a persona can be under
    one cap and over the other)."""
    line_count = len(body.splitlines())
    if line_count > _MAX_RENDERED_LINES:
        raise PersonaError(
            f"persona renders to {line_count} lines; the limit is {_MAX_RENDERED_LINES}"
        )
    byte_count = len(body.encode("utf-8"))
    if byte_count > _MAX_RENDERED_BYTES:
        raise PersonaError(
            f"persona renders to {byte_count} bytes; the limit is {_MAX_RENDERED_BYTES}"
        )


def check_substance(fields: PersonaFields, *, rules_required: bool = True) -> None:
    """Raise PersonaError when `fields` is too thin to be a persona an
    agent can work from: under `PERSONA_WHAT_MIN_CHARS` of what-they-do, or,
    when `rules_required`, fewer than `PERSONA_RULES_MIN` rules. Checked at
    create and at edit, for the form and the MCP tool alike; never on read,
    so a thin persona already in the catalog keeps working until somebody
    edits it. The routes pass `rules_required` off in a room with guardrails
    on, where an agent's rules live in its Guardrails instead."""
    if len(fields.what_they_do) < PERSONA_WHAT_MIN_CHARS:
        raise PersonaError(
            "What they do is too short to work from: say what the persona covers, "
            "how it works through a request and what it hands back "
            f"({PERSONA_WHAT_MIN_CHARS} characters at least, it has "
            f"{len(fields.what_they_do)})"
        )
    if rules_required and len(fields.rules) < PERSONA_RULES_MIN:
        raise PersonaError(
            f"a persona needs at least {PERSONA_RULES_MIN} rules, one line each: "
            "things it always or never does"
        )


def _read_index(storage: StorageClient, bucket: str, prefix: str) -> list[dict]:
    """The current `<prefix>index.json` entries, or `[]` when the object
    isn't there yet (a bucket that's never had a persona published)."""
    try:
        data = storage.read_bytes(bucket, f"{prefix}index.json")
    except StorageNotFound:
        return []
    parsed = json.loads(data)
    return list(parsed.get("entries", []))


read_index = _read_index
"""Public alias of `_read_index` for route callers outside this module
(the underscore original stays for in-module callers)."""


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


def set_persona_archived(
    storage: StorageClient,
    *,
    bucket: str,
    prefix: str = "",
    persona_id: str,
    archived: bool,
) -> dict:
    """Flip one index entry's archived flag (hardening wave: archive, not
    delete -- the persona.md object is never touched, and unarchive
    restores full standing). Archived is presence-based: setting True
    writes `"archived": True`; setting False REMOVES the key, so every
    pre-archive index file stays valid and absence always means active.
    Returns the updated entry. Raises PersonaError when `persona_id` is
    not in the index (no anti-enumeration fold here: the persona catalog
    is room-readable, honesty wins)."""
    index = _read_index(storage, bucket, prefix)
    for entry in index:
        if entry["id"] == persona_id:
            if archived:
                entry["archived"] = True
            else:
                entry.pop("archived", None)
            _write_index(storage, bucket, prefix, index)
            return entry
    raise PersonaError(f"no persona with id {persona_id!r}")


def set_persona_team(
    storage: StorageClient,
    *,
    bucket: str,
    prefix: str = "",
    persona_id: str,
    team: str,
) -> dict:
    """Re-file one index entry under `team`, touching nothing else on it:
    name, description, tags, builtin, created_by and any archived flag all
    survive as they were, and the persona.md object is never rewritten.
    Returns the updated entry. Raises PersonaError when `persona_id` is not
    in the index, same honesty rule as `set_persona_archived` above."""
    index = _read_index(storage, bucket, prefix)
    for entry in index:
        if entry["id"] == persona_id:
            entry["team"] = team
            _write_index(storage, bucket, prefix, index)
            return entry
    raise PersonaError(f"no persona with id {persona_id!r}")


def create_persona(
    storage: StorageClient,
    *,
    bucket: str,
    prefix: str = "",
    fields: PersonaFields,
    team: str,
    created_by: str | None = None,
    rules_required: bool = True,
) -> CatalogEntry:
    """Write a room-authored persona: storage only, never touches search.

    `created_by`, when known, is the creating caller's verified email,
    lowercased into the index entry: the ownership axis `PATCH
    /api/personas/{id}` checks (the person who made a persona may re-file
    it). Absent when the room has no sign-in to say who asked.

    `search.ingest_indexes` does a full catalog rebuild from every
    configured bucket at once (Task 3) — calling it here with just the
    personas bucket would silently wipe every skill entry out of the FTS
    table. The route (`kobo_api.main`) re-ingests with the FULL
    kind->bucket mapping right after this returns successfully (Task 4
    controller decision #1 — the change that closes that hazard; see
    test_api_personas.py's regression test proving a pre-existing skill
    entry survives a persona create).

    Raises `PersonaError` if `fields.name` has more than
    `PERSONA_NAME_MAX_WORDS` words (short-names ruling), doesn't slugify to a valid id, if
    that name's rendered persona.md exceeds the room's real ceiling (200
    lines or 49152 bytes, checked by `_check_rendered_size` against the
    ACTUAL rendered body — never a per-field lookalike), or `PersonaExists`
    if the id is already in the index. Tags are `fields.purpose_tags`,
    normalized and capped — never derived (create-flow polish, 2026-08-28;
    `_derive_tags` is retired).
    """
    check_name_length(fields.name)
    id_ = persona_id(fields.name)

    index = _read_index(storage, bucket, prefix)
    existing = next((entry for entry in index if entry["id"] == id_), None)
    if existing is not None:
        raise PersonaExists(f"a persona with id {id_!r} already exists", existing)

    check_substance(fields, rules_required=rules_required)
    body = render_persona(fields)
    _check_rendered_size(body)
    storage.write_bytes(
        bucket,
        f"{prefix}{id_}/persona.md",
        body.encode(),
        metadata={"team": team, "creator": "room"},
        content_type="text/markdown",
    )

    tags = normalize_tags(fields.purpose_tags)[:_TAG_CAP]
    entry = {
        "id": id_,
        "name": fields.name,
        "description": fields.who_they_are,
        "team": team,
        "tags": tags,
        "builtin": False,
    }
    if created_by:
        entry["created_by"] = created_by.lower()
    index.append(entry)
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


def update_persona(
    storage: StorageClient,
    *,
    bucket: str,
    prefix: str = "",
    persona_id: str,
    who_they_are: str,
    what_they_do: str,
    rules: list[str],
    updated_by: str | None = None,
    rules_required: bool = True,
) -> dict:
    """Rewrite a room-authored persona's text in place: who they are, what
    they do, the rules. The id, the name, the group, the tags and the maker
    stay as they were, so nothing that points at the persona moves. Storage
    only, same as `create_persona`: the route re-ingests.

    The same cleaners and the same floor as a create (`PersonaFields`,
    `check_substance`, `_check_rendered_size`), so an edit can only leave a
    persona in the house shape. An agent already made from the persona
    keeps the copy it was made with; the edit is what the next agent gets.

    Only a persona whose stored text is in the form's shape is edited. A
    team guide (`upsert_persona`) or a body somebody put in the bucket by
    hand has no `## What you do` for the form to read back, so the form
    would open on the one-line description and saving it would replace the
    whole text with that line.

    Raises `PersonaError` when the id is not in the index, when it is a
    built-in (the boot-time seed would write the packaged text back) or
    archived, when its stored text is not in the form's shape, or when the
    new text fails a check. `pydantic.ValidationError`
    from the fields is the caller's to turn into a 422.
    """
    index = _read_index(storage, bucket, prefix)
    entry = next((e for e in index if e["id"] == persona_id), None)
    if entry is None or entry.get("archived"):
        raise PersonaError(f"no persona with id {persona_id!r}")
    if entry.get("builtin"):
        raise PersonaError("built-in personas are the room's own and are not edited")

    key = f"{prefix}{persona_id}/persona.md"
    try:
        stored = parse_persona_body(storage.read_bytes(bucket, key).decode())
    except StorageNotFound:
        stored = ParsedPersona()
    if stored.what_they_do is None:
        raise PersonaError(
            "this persona was not written with the persona form, so it is not edited here"
        )
    # Writing over the file gives it a new date and new custom metadata.
    # The date it was first made is read off the old file once and kept in
    # the index, where the single-persona read looks first.
    try:
        meta = storage.object_metadata(bucket, key)
    except ClientError:
        meta = {}
    creator = (meta.get("metadata") or {}).get("creator") or "room"
    made_at = meta.get("timeCreated") or meta.get("updated")

    fields = PersonaFields(
        name=entry.get("name") or persona_id,
        who_they_are=who_they_are,
        what_they_do=what_they_do,
        rules=rules,
    )
    check_substance(fields, rules_required=rules_required)
    body = render_persona(fields)
    _check_rendered_size(body)
    storage.write_bytes(
        bucket,
        key,
        body.encode(),
        metadata={"team": entry.get("team", ""), "creator": creator},
        content_type="text/markdown",
    )

    entry["description"] = fields.who_they_are
    # Always set: it is also how the boot-time seed knows a packaged group
    # persona was edited in the room and leaves its line alone.
    entry["updated_by"] = (updated_by or "").lower() or "room"
    if made_at and not entry.get("created_at"):
        entry["created_at"] = made_at
    _write_index(storage, bucket, prefix, index)
    return entry


def upsert_persona(
    storage: StorageClient,
    *,
    bucket: str,
    prefix: str = "",
    id: str,
    name: str,
    description: str,
    body: str,
    team: str,
    tags: list[str] | None = None,
    creator: str = "room",
) -> CatalogEntry:
    """Write an ALREADY-RENDERED persona.md under `id`, overwriting whatever
    is there — same storage layout, same index shape, and the same
    never-touches-search contract as `create_persona` above (its caller
    re-ingests with the FULL kind->bucket mapping right after).

    Two deliberate differences from `create_persona`, both required by the
    archetype create lane (kobo_api.archetypes): the body arrives rendered
    (an archetype persona comes from a packaged template, not from
    `PersonaFields`), and an existing id is REPLACED rather than refused —
    a team's guide persona id is derived from its group, so re-creating a
    guide after a destroy must re-render in place instead of hitting
    `PersonaExists`. Replacing the index entry outright also clears a stale
    `archived` flag, which is the honest reading: the persona is being
    written afresh.

    The rendered ceiling (`_check_rendered_size`) still applies — a packaged
    template is not exempt from the room's persona.md limits.
    """
    _check_rendered_size(body)
    storage.write_bytes(
        bucket,
        f"{prefix}{id}/persona.md",
        body.encode(),
        metadata={"team": team, "creator": creator},
        content_type="text/markdown",
    )

    cleaned = normalize_tags(tags or [])[:_TAG_CAP]
    index = [entry for entry in _read_index(storage, bucket, prefix) if entry["id"] != id]
    index.append(
        {
            "id": id,
            "name": name,
            "description": description,
            "team": team,
            "tags": cleaned,
            "builtin": False,
        }
    )
    _write_index(storage, bucket, prefix, index)

    return CatalogEntry(
        kind="persona",
        id=id,
        name=name,
        description=description,
        team=team,
        tags=cleaned,
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

        team = TEAM_TEMPLATES.get(info.id, "shared")
        if exists:
            skipped += 1
        else:
            storage.write_bytes(
                bucket,
                key,
                path.read_bytes(),
                metadata={"team": team, "creator": "kobo"},
                content_type="text/markdown",
            )
            seeded += 1

        # "name" has no separate concept in the packaged templates (only id
        # + description are parsed from them) — the id itself is used as
        # the display name, same as how these ship with no other naming
        # convention to draw from.
        previous = index_by_id.get(info.id)
        # The entry is rewritten every boot, so a template moved from the
        # commons to a group (TEAM_TEMPLATES) moves in the index on the next
        # restart, and a room that seeded it as a built-in earlier converges
        # without anyone editing storage.
        index_by_id[info.id] = {
            "id": info.id,
            "name": info.id,
            "description": info.description,
            "team": team,
            "tags": [],
            "builtin": info.id not in TEAM_TEMPLATES,
        }
        # The resurrect trap (hardening wave): this rewrite runs every
        # boot; without carrying the flag forward, an archived builtin
        # would silently return on the next restart.
        if previous and previous.get("archived"):
            index_by_id[info.id]["archived"] = True
        # A packaged group persona edited in the room (`update_persona`)
        # keeps its edited file, above; its one line in the list and who
        # edited it stay with it, or the list would show the packaged
        # description over the edited text after every restart.
        if previous and previous.get("updated_by") and not index_by_id[info.id]["builtin"]:
            for kept in ("description", "updated_by", "created_at"):
                if previous.get(kept):
                    index_by_id[info.id][kept] = previous[kept]

    for retired in RETIRED_TEMPLATES:
        if retired in index_by_id:
            index_by_id[retired]["archived"] = True
    _write_index(storage, bucket, prefix, list(index_by_id.values()))
    return {"seeded": seeded, "skipped": skipped}
