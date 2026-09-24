"""Unit tests for kobo_api.personas: persona_id slugification, the
deterministic render_persona template (Persona builder v2's four-field
"house skeleton"), server-side tag derivation, and the storage-only
create_persona/seed_default_personas functions — no FastAPI app involved.

See test_api_personas.py for the route-level tests, including the
ingest-hazard regression (Task 4 controller decision #1: create_persona
must never call search.ingest_indexes itself).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import kobo_api.personas as personas_module
from kobo_api.clients import StorageNotFound
from kobo_api.personas import (
    BUILTIN_ORDER,
    in_reading_order,
    PersonaError,
    PersonaExists,
    PersonaFields,
    check_name_length,
    create_persona,
    parse_persona_body,
    persona_id,
    render_persona,
    seed_default_personas,
    set_persona_archived,
)
from tests.fakes import FakeStorage

# Every packaged built-in the seed writes (templates/personas/*.md).
_PACKAGED_IDS = {p.stem for p in personas_module._PACKAGE_PERSONAS_DIR.glob("*.md")}


def _fields(**overrides) -> PersonaFields:
    base = {
        "name": "Aria the Assistant",
        "who_they_are": "a calm troubleshooter for billing questions",
        "what_they_do": "Help customers fix billing problems quickly.",
    }
    base.update(overrides)
    return PersonaFields(**base)


# --- persona_id --------------------------------------------------------


def test_persona_id_spacing_and_case():
    assert persona_id("Aria the Assistant") == "aria-the-assistant"


def test_persona_id_punctuation_collapses_to_single_dash():
    assert persona_id("Dr. Helper!!") == "dr-helper"


def test_persona_id_caps_at_40_chars():
    assert persona_id("a" * 50) == "a" * 40


def test_persona_id_truncation_can_end_in_a_dash():
    # The 40th character of the slugified name lands exactly on a
    # separator dash — cap(40) is applied AFTER strip, per the brief's
    # exact order ("lower, non-alnum runs -> '-', strip, cap 40"), so a
    # trailing dash surviving truncation is expected, not an error:
    # PERSONA_ID_RE has no anti-trailing-hyphen rule (unlike catalog.NAME_RE).
    name = "a" * 39 + " long tail"
    assert persona_id(name) == "a" * 39 + "-"


def test_persona_id_leading_digit_is_invalid():
    with pytest.raises(PersonaError):
        persona_id("123 cool")


def test_persona_id_only_punctuation_is_invalid():
    with pytest.raises(PersonaError):
        persona_id("!!!")


# --- render_persona (byte-pinned golden, Persona builder v2 house skeleton) --

FULL_FIELDS = PersonaFields(
    name="Aria the Assistant",
    who_they_are="an unflappable support lead who's talked down a thousand outages",
    what_they_do="Help customers troubleshoot billing issues and escalate when needed.",
    rules=["Never share other customers' data.", "Escalate refunds over $500."],
)

FULL_EXPECTED = (
    "You are {name}, an unflappable support lead who's talked down a thousand outages.\n"
    "\n"
    "Speak in the first person, plainly. Lead with the answer; skip\n"
    "throat-clearing. Say \"I don't know yet\" when it's true.\n"
    "\n"
    "## What you do\n"
    "\n"
    "Help customers troubleshoot billing issues and escalate when needed.\n"
    "\n"
    "## Rules\n"
    "\n"
    "- Never share other customers' data.\n"
    "- Escalate refunds over $500.\n"
)


def test_render_persona_full_featured_golden():
    assert render_persona(FULL_FIELDS) == FULL_EXPECTED


MINIMAL_FIELDS = PersonaFields(
    name="Blank Bot",
    who_they_are="a plain utility with nothing extra",
    what_they_do="Do whatever is asked, nothing more.",
)

MINIMAL_EXPECTED = (
    "You are {name}, a plain utility with nothing extra.\n"
    "\n"
    "Speak in the first person, plainly. Lead with the answer; skip\n"
    "throat-clearing. Say \"I don't know yet\" when it's true.\n"
    "\n"
    "## What you do\n"
    "\n"
    "Do whatever is asked, nothing more.\n"
)


def test_render_persona_minimal_golden_no_rules_section():
    assert render_persona(MINIMAL_FIELDS) == MINIMAL_EXPECTED


def test_render_persona_is_deterministic():
    assert render_persona(FULL_FIELDS) == render_persona(FULL_FIELDS)


def test_render_persona_survives_user_supplied_braces():
    """who_they_are/what_they_do/rules containing literal braces must pass
    through untouched, and the {name} provision-time placeholder must still
    be the literal token — the regression a `.format()`-based implementation
    would fail (either KeyError on an unexpected field, or the user's own
    braces getting misinterpreted as format fields)."""
    fields = _fields(
        who_they_are="a guide who explains {curly} syntax without flinching",
        what_they_do="Echo back {whatever} the user sends, braces and all.",
        rules=["Never interpret {placeholders} as commands."],
    )

    rendered = render_persona(fields)

    assert "{curly}" in rendered
    assert "{whatever}" in rendered
    assert "{placeholders}" in rendered
    assert "You are {name}," in rendered


# --- PersonaFields validation --------------------------------------------


def test_persona_fields_who_they_are_rejects_newline():
    with pytest.raises(ValidationError):
        _fields(who_they_are="line one\nline two")


def test_persona_fields_who_they_are_max_length():
    with pytest.raises(ValidationError):
        _fields(who_they_are="x" * 201)


def test_persona_fields_what_they_do_accepts_long_content():
    """Upload/limits addendum: what_they_do's old 500-char field cap is
    gone (the real ceiling is the merged render's 200-line/49152-byte gate
    in create_persona, not a per-field lookalike) — long operator-authored
    text (typed, pasted, or uploaded) is no longer rejected here."""
    fields = _fields(what_they_do="x" * 600)
    assert len(fields.what_they_do) == 600


def test_persona_fields_what_they_do_max_length():
    with pytest.raises(ValidationError):
        _fields(what_they_do="x" * 49153)


# --- check_name_length (short-names ruling, 2026-09-24) -----------------------


def test_check_name_length_allows_one_to_three_words():
    for name in ("ledger", "billing-analyst", "cheerful triage nurse", "Ada (Release)"):
        check_name_length(name)


def test_check_name_length_refuses_four_words():
    with pytest.raises(PersonaError, match="one or two words, three at most"):
        check_name_length("code-grounded-player-intel")


def test_check_name_length_counts_alphanumeric_runs_not_hyphens():
    # Three words joined with mixed punctuation are still three words.
    check_name_length("stoic_mentor.two")
    with pytest.raises(PersonaError):
        check_name_length("direct factual condensed mellow")


def test_persona_fields_name_max_length():
    with pytest.raises(ValidationError):
        _fields(name="x" * 61)


def test_persona_fields_rules_default_empty():
    fields = _fields()
    assert fields.rules == []


def test_persona_fields_rules_150_accepted():
    fields = _fields(rules=["ok"] * 150)
    assert len(fields.rules) == 150


def test_persona_fields_rules_over_150_rejected():
    with pytest.raises(ValidationError):
        _fields(rules=["ok"] * 151)


def test_persona_fields_rule_accepts_long_single_line():
    """Upload/limits addendum: the old 200-char per-rule cap is gone — a
    long single rule is bounded by the merged render's own gate
    (create_persona), not a per-field lookalike of it."""
    fields = _fields(rules=["x" * 300])
    assert fields.rules == ["x" * 300]


def test_persona_fields_rule_rejects_empty():
    with pytest.raises(ValidationError):
        _fields(rules=[""])


def test_persona_fields_rule_rejects_newline():
    with pytest.raises(ValidationError):
        _fields(rules=["line one\nline two"])


def test_persona_fields_no_longer_accepts_tags():
    """Tags are server-derived now (Persona builder v2) — a caller passing
    `tags` explicitly must not silently smuggle it through as a client-
    authored value; pydantic simply doesn't know the field any more."""
    fields = _fields()
    assert not hasattr(fields, "tags")


# --- tag derivation (Persona builder v2: auto-derived, field hidden) -------


def test_create_persona_writes_body_and_index_with_exact_keys_and_metadata():
    storage = FakeStorage()
    fields = _fields()

    entry = create_persona(storage, bucket="personas-bucket", fields=fields, team="cs-team")

    # Create-flow polish (2026-08-28): tags are supplied-only -- _fields()
    # passes none, so the entry carries none. The retired derivation used
    # to invent six from the persona's own nouns.
    expected_tags = []

    assert entry.kind == "persona"
    assert entry.id == "aria-the-assistant"
    assert entry.name == "Aria the Assistant"
    assert entry.description == "a calm troubleshooter for billing questions"
    assert entry.team == "cs-team"
    assert entry.tags == expected_tags
    assert entry.builtin is False

    body = storage.read_bytes("personas-bucket", "aria-the-assistant/persona.md")
    assert body == render_persona(fields).encode()

    meta = storage.object_metadata("personas-bucket", "aria-the-assistant/persona.md")
    assert meta["metadata"] == {"team": "cs-team", "creator": "room"}

    index = json.loads(storage.read_bytes("personas-bucket", "index.json"))
    assert index["entries"] == [
        {
            "id": "aria-the-assistant",
            "name": "Aria the Assistant",
            "description": "a calm troubleshooter for billing questions",
            "team": "cs-team",
            "tags": expected_tags,
            "builtin": False,
        }
    ]


def test_create_persona_respects_prefix():
    storage = FakeStorage()
    fields = _fields()

    create_persona(
        storage, bucket="personas-bucket", prefix="orgs/acme/", fields=fields, team="cs-team"
    )

    # raises StorageNotFound (via the un-prefixed read below) if the prefix
    # wasn't actually honored for either object.
    storage.read_bytes("personas-bucket", "orgs/acme/aria-the-assistant/persona.md")
    storage.read_bytes("personas-bucket", "orgs/acme/index.json")


def test_create_persona_duplicate_raises_persona_exists():
    storage = FakeStorage()
    fields = _fields()
    create_persona(storage, bucket="personas-bucket", fields=fields, team="cs-team")

    with pytest.raises(PersonaExists):
        create_persona(storage, bucket="personas-bucket", fields=fields, team="cs-team")


def test_create_persona_bad_slug_raises_persona_error():
    storage = FakeStorage()
    fields = _fields(name="!!!")

    with pytest.raises(PersonaError):
        create_persona(storage, bucket="personas-bucket", fields=fields, team="cs-team")


def test_create_persona_appends_to_existing_index_without_disturbing_it():
    storage = FakeStorage()
    create_persona(storage, bucket="personas-bucket", fields=_fields(), team="cs-team")

    other = _fields(name="Blaine the Bot")
    create_persona(storage, bucket="personas-bucket", fields=other, team="ops-team")

    index = json.loads(storage.read_bytes("personas-bucket", "index.json"))
    ids = sorted(e["id"] for e in index["entries"])
    assert ids == ["aria-the-assistant", "blaine-the-bot"]


# --- create_persona: the merged render's real size gate (upload/limits addendum) ---
#
# The room's real ceiling is the MERGED persona.md — render_persona's output,
# body plus rules section together — never a per-field lookalike: 200 lines
# and 49152 bytes, checked against the actual rendered body. These tests pin
# both boundaries exactly (N ok, N+1 rejected), the same way persona_id's own
# 40-char cap is pinned above.


def test_create_persona_accepts_exactly_200_lines():
    storage = FakeStorage()
    # render_persona produces 7 fixed lines (before what_they_do), so 200 total requires 193 lines in what_they_do
    what_they_do_193_lines = "\n".join(["line"] * 193)
    fields = _fields(what_they_do=what_they_do_193_lines)
    assert len(render_persona(fields).splitlines()) == 200

    entry = create_persona(storage, bucket="personas-bucket", fields=fields, team="cs-team")

    assert entry.id == "aria-the-assistant"


def test_create_persona_rejects_201_lines():
    storage = FakeStorage()
    # render_persona produces 7 fixed lines, so 201 total requires 194 lines in what_they_do
    what_they_do_194_lines = "\n".join(["line"] * 194)
    fields = _fields(what_they_do=what_they_do_194_lines)
    assert len(render_persona(fields).splitlines()) == 201

    with pytest.raises(PersonaError, match="persona renders to 201 lines; the limit is 200"):
        create_persona(storage, bucket="personas-bucket", fields=fields, team="cs-team")


def _what_they_do_for_total_bytes(total: int) -> str:
    """A what_they_do value that makes render_persona's output exactly
    `total` bytes for fields otherwise matching _fields()'s defaults (no
    rules, an all-ASCII skeleton, so byte length == char length
    throughout)."""
    overhead = len(render_persona(_fields(what_they_do="x")).encode()) - 1
    return "x" * (total - overhead)


def test_create_persona_accepts_exactly_49152_bytes():
    storage = FakeStorage()
    fields = _fields(what_they_do=_what_they_do_for_total_bytes(49152))
    assert len(render_persona(fields).encode()) == 49152

    entry = create_persona(storage, bucket="personas-bucket", fields=fields, team="cs-team")

    assert entry.id == "aria-the-assistant"


def test_create_persona_rejects_49153_bytes():
    storage = FakeStorage()
    fields = _fields(what_they_do=_what_they_do_for_total_bytes(49153))
    assert len(render_persona(fields).encode()) == 49153

    with pytest.raises(
        PersonaError, match="persona renders to 49153 bytes; the limit is 49152"
    ):
        create_persona(storage, bucket="personas-bucket", fields=fields, team="cs-team")


# --- seed_default_personas -------------------------------------------------


def _packaged_path(persona_id_: str) -> Path:
    return Path(personas_module.__file__).parent / "templates" / "personas" / f"{persona_id_}.md"


def test_seed_default_personas_first_run_writes_all():
    storage = FakeStorage()

    result = seed_default_personas(storage, bucket="personas-bucket")

    assert result == {"seeded": len(_PACKAGED_IDS), "skipped": 0}

    index = json.loads(storage.read_bytes("personas-bucket", "index.json"))
    ids = {e["id"] for e in index["entries"]}
    assert ids == _PACKAGED_IDS
    for entry in index["entries"]:
        assert entry["builtin"] is True
        assert entry["team"] == "shared"
        assert entry["tags"] == []

    meta = storage.object_metadata("personas-bucket", "helpful-generalist/persona.md")
    assert meta["metadata"] == {"team": "shared", "creator": "kobo"}


def test_seed_default_personas_no_longer_seeds_blank():
    """Persona builder v2: "blank" confuses (operator feedback) and is
    dropped from the packaged defaults entirely; the baseline built-ins
    ship instead."""
    storage = FakeStorage()

    seed_default_personas(storage, bucket="personas-bucket")

    with pytest.raises(StorageNotFound):
        storage.read_bytes("personas-bucket", "blank/persona.md")


def test_seed_default_personas_preserves_packaged_file_bytes():
    storage = FakeStorage()
    seed_default_personas(storage, bucket="personas-bucket")

    written = storage.read_bytes("personas-bucket", "helpful-generalist/persona.md")
    assert written == _packaged_path("helpful-generalist").read_bytes()
    # the agent-name placeholder must survive seeding untouched.
    assert b"{name}" in written


def test_seed_default_personas_second_run_all_skipped():
    storage = FakeStorage()
    seed_default_personas(storage, bucket="personas-bucket")

    result = seed_default_personas(storage, bucket="personas-bucket")

    assert result == {"seeded": 0, "skipped": len(_PACKAGED_IDS)}


def test_seed_default_personas_never_overwrites_existing_object():
    storage = FakeStorage()
    custom_body = b"<!-- description: hand-edited -->\ncustom body\n"
    storage.write_bytes(
        "personas-bucket",
        "helpful-generalist/persona.md",
        custom_body,
        metadata={"team": "shared", "creator": "an-operator"},
        content_type="text/markdown",
    )

    seed_default_personas(storage, bucket="personas-bucket")

    assert storage.read_bytes("personas-bucket", "helpful-generalist/persona.md") == custom_body


def test_seed_default_personas_merges_with_preexisting_room_authored_entries():
    """seed_default_personas must merge into index.json, not clobber it —
    a room-authored persona already in the index must survive a seed run."""
    storage = FakeStorage()
    create_persona(storage, bucket="personas-bucket", fields=_fields(), team="cs-team")

    seed_default_personas(storage, bucket="personas-bucket")

    index = json.loads(storage.read_bytes("personas-bucket", "index.json"))
    ids = {e["id"] for e in index["entries"]}
    assert ids == {"aria-the-assistant"} | _PACKAGED_IDS


# --- set_persona_archived (hardening wave: archive, not delete) --------


def test_set_persona_archived_flags_and_unflags():
    storage = FakeStorage()
    storage.write_bytes("b", "index.json", json.dumps({"entries": [
        {"id": "wire-probe", "name": "Wire Probe", "description": "d",
         "team": "shared", "tags": [], "builtin": False},
    ]}).encode())
    entry = set_persona_archived(storage, bucket="b", persona_id="wire-probe", archived=True)
    assert entry["archived"] is True
    index = json.loads(storage.read_bytes("b", "index.json"))
    assert index["entries"][0]["archived"] is True

    entry = set_persona_archived(storage, bucket="b", persona_id="wire-probe", archived=False)
    assert "archived" not in entry
    index = json.loads(storage.read_bytes("b", "index.json"))
    assert "archived" not in index["entries"][0]


def test_set_persona_archived_unknown_id_raises():
    storage = FakeStorage()
    storage.write_bytes("b", "index.json", json.dumps({"entries": []}).encode())
    with pytest.raises(PersonaError, match="no persona"):
        set_persona_archived(storage, bucket="b", persona_id="ghost", archived=True)


def test_seed_default_personas_preserves_archived_flag():
    """The resurrect trap: seeding rewrites builtin index entries wholesale
    every boot; an archived builtin must stay archived across a restart."""
    storage = FakeStorage()
    seed_default_personas(storage, bucket="b")
    set_persona_archived(storage, bucket="b", persona_id="helpful-generalist", archived=True)
    seed_default_personas(storage, bucket="b")  # the restart
    index = json.loads(storage.read_bytes("b", "index.json"))
    entry = next(e for e in index["entries"] if e["id"] == "helpful-generalist")
    assert entry.get("archived") is True


# --- parse_persona_body: the inverse of render_persona ---------------------


def test_parse_persona_body_round_trips_render_persona():
    """The contract this parser exists for: anything render_persona wrote,
    parse_persona_body reads back field for field."""
    fields = _fields(
        what_they_do="Help customers fix billing problems quickly.\n\nEscalate anything that smells like fraud.",
        rules=["Never share account numbers.", "Say when you do not know."],
    )

    parsed = parse_persona_body(render_persona(fields))

    assert parsed.who_they_are == fields.who_they_are
    assert parsed.what_they_do == fields.what_they_do
    assert parsed.rules == fields.rules


def test_parse_persona_body_round_trips_without_rules():
    fields = _fields()

    parsed = parse_persona_body(render_persona(fields))

    assert parsed.who_they_are == fields.who_they_are
    assert parsed.what_they_do == fields.what_they_do
    assert parsed.rules == []


def test_parse_persona_body_ignores_a_description_header_line():
    """The packaged templates lead with `<!-- description: ... -->`; a
    rendered body that has had one prepended still parses."""
    fields = _fields()
    text = "<!-- description: a calm troubleshooter -->\n" + render_persona(fields)

    parsed = parse_persona_body(text)

    assert parsed.who_they_are == fields.who_they_are
    assert parsed.what_they_do == fields.what_they_do


def test_parse_persona_body_sectionless_template_yields_nothing():
    """A packaged built-in has no "## What you do" heading at all — the
    parser says so (None) rather than guessing a body out of the prose."""
    # The opening line reads like a who-they-are and even ends in a period.
    # Without the heading it is still just prose, and gets no special
    # reading at all.
    text = (
        "You are {name}, a helpful assistant.\n"
        "Be concise, be warm, and say so plainly when you do not know something.\n"
    )

    parsed = parse_persona_body(text)

    assert parsed.who_they_are is None
    assert parsed.what_they_do is None
    assert parsed.rules == []


def test_parse_persona_body_keeps_braces_in_the_body():
    """Free-form persona text can carry literal braces; nothing here runs
    the body back through str.format, so they survive."""
    fields = _fields(what_they_do="Read the {payload} and answer.")

    parsed = parse_persona_body(render_persona(fields))

    assert parsed.what_they_do == "Read the {payload} and answer."


# --- in_reading_order --------------------------------------------------------------


def _entry(id_, *, builtin=False, team="shared"):
    return {"id": id_, "name": id_, "description": "", "team": team, "tags": [], "builtin": builtin}


def test_reading_order_puts_builtins_first_by_the_curated_list_then_teams_by_name():
    entries = [
        _entry("zed-team", team="sre"),
        _entry("gcp-operator", builtin=True),
        _entry("alpha-team", team="qa"),
        _entry("helpful-generalist", builtin=True),
        _entry("code-reviewer", builtin=True),
    ]
    ordered = [e["id"] for e in in_reading_order(entries)]
    assert ordered == [
        "helpful-generalist",
        "code-reviewer",
        "gcp-operator",
        "alpha-team",
        "zed-team",
    ]


def test_reading_order_places_an_unlisted_builtin_after_the_listed_ones_by_name():
    entries = [
        _entry("zz-new-builtin", builtin=True),
        _entry("aa-new-builtin", builtin=True),
        _entry("gcp-operator", builtin=True),
    ]
    ordered = [e["id"] for e in in_reading_order(entries)]
    assert ordered == ["gcp-operator", "aa-new-builtin", "zz-new-builtin"]


def test_reading_order_is_stable_for_a_list_already_in_order():
    entries = [_entry(id_, builtin=True) for id_ in BUILTIN_ORDER]
    assert [e["id"] for e in in_reading_order(entries)] == list(BUILTIN_ORDER)
