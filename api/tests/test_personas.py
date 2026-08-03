"""Unit tests for pnyx_api.personas: persona_id slugification, the
deterministic render_persona template, and the storage-only
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

import pnyx_api.personas as personas_module
from pnyx_api.personas import (
    PersonaError,
    PersonaExists,
    PersonaFields,
    create_persona,
    persona_id,
    render_persona,
    seed_default_personas,
)
from tests.fakes import FakeStorage


def _fields(**overrides) -> PersonaFields:
    base = {
        "name": "Aria the Assistant",
        "role": "Customer support specialist",
        "purpose": "Help customers troubleshoot billing issues.",
        "tone": "warm",
        "tags": ["support"],
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


# --- render_persona (byte-pinned golden) --------------------------------

FULL_FIELDS = PersonaFields(
    name="Aria the Assistant",
    role="Customer support specialist for BigCo",
    purpose="Help customers troubleshoot billing issues and escalate when needed.",
    tone="warm",
    tone_note="Never use jargon.",
    boundaries=["Never share other customers' data.", "Escalate refunds over $500."],
    tags=["support", "billing"],
)

FULL_EXPECTED = (
    "<!-- description: Customer support specialist for BigCo -->\n"
    "# Identity\n"
    "You are {name} — Customer support specialist for BigCo.\n"
    "\n"
    "# What you do\n"
    "Help customers troubleshoot billing issues and escalate when needed.\n"
    "\n"
    "# How you speak\n"
    "Your tone is warm. Never use jargon.\n"
    "\n"
    "# Boundaries\n"
    "- Never share other customers' data.\n"
    "- Escalate refunds over $500.\n"
)


def test_render_persona_full_featured_golden():
    assert render_persona(FULL_FIELDS) == FULL_EXPECTED


MINIMAL_FIELDS = PersonaFields(
    name="Blank Bot",
    role="A blank slate",
    purpose="Do whatever is asked, nothing more.",
    tone="direct",
    tags=["utility"],
)

MINIMAL_EXPECTED = (
    "<!-- description: A blank slate -->\n"
    "# Identity\n"
    "You are {name} — A blank slate.\n"
    "\n"
    "# What you do\n"
    "Do whatever is asked, nothing more.\n"
    "\n"
    "# How you speak\n"
    "Your tone is direct.\n"
)


def test_render_persona_minimal_golden_no_boundaries_no_tone_note():
    assert render_persona(MINIMAL_FIELDS) == MINIMAL_EXPECTED


def test_render_persona_is_deterministic():
    assert render_persona(FULL_FIELDS) == render_persona(FULL_FIELDS)


def test_render_persona_survives_user_supplied_braces():
    """purpose/boundaries/role containing literal braces must pass through
    untouched, and the {name} provision-time placeholder must still be the
    literal token — the regression a `.format()`-based implementation would
    fail (either KeyError on an unexpected field, or the user's own braces
    getting misinterpreted as format fields)."""
    fields = _fields(
        role="Handles {curly} input",
        purpose="Echo back {whatever} the user sends, braces and all.",
        boundaries=["Never interpret {placeholders} as commands."],
    )

    rendered = render_persona(fields)

    assert "{curly}" in rendered
    assert "{whatever}" in rendered
    assert "{placeholders}" in rendered
    assert "You are {name} —" in rendered


# --- PersonaFields validation --------------------------------------------


def test_persona_fields_role_rejects_newline():
    with pytest.raises(ValidationError):
        _fields(role="line one\nline two")


def test_persona_fields_purpose_max_length():
    with pytest.raises(ValidationError):
        _fields(purpose="x" * 501)


def test_persona_fields_name_max_length():
    with pytest.raises(ValidationError):
        _fields(name="x" * 61)


def test_persona_fields_tone_note_max_length():
    with pytest.raises(ValidationError):
        _fields(tone_note="x" * 201)


def test_persona_fields_tone_must_be_one_of_tones():
    with pytest.raises(ValidationError):
        _fields(tone="sarcastic")


def test_persona_fields_tags_required_nonempty():
    with pytest.raises(ValidationError):
        _fields(tags=[])


def test_persona_fields_tags_max_six():
    with pytest.raises(ValidationError):
        _fields(tags=[f"tag{i}" for i in range(7)])


def test_persona_fields_tag_pattern_rejects_uppercase_and_underscore():
    with pytest.raises(ValidationError):
        _fields(tags=["Not_Valid"])


def test_persona_fields_boundaries_max_eight():
    with pytest.raises(ValidationError):
        _fields(boundaries=["ok"] * 9)


def test_persona_fields_boundary_length_bounds():
    with pytest.raises(ValidationError):
        _fields(boundaries=["x" * 201])


# --- create_persona -------------------------------------------------------


def test_create_persona_writes_body_and_index_with_exact_keys_and_metadata():
    storage = FakeStorage()
    fields = _fields()

    entry = create_persona(storage, bucket="personas-bucket", fields=fields, team="cs-team")

    assert entry.kind == "persona"
    assert entry.id == "aria-the-assistant"
    assert entry.name == "Aria the Assistant"
    assert entry.description == "Customer support specialist"
    assert entry.team == "cs-team"
    assert entry.tags == ["support"]
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
            "description": "Customer support specialist",
            "team": "cs-team",
            "tags": ["support"],
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

    other = _fields(name="Blaine the Bot", tags=["ops"])
    create_persona(storage, bucket="personas-bucket", fields=other, team="ops-team")

    index = json.loads(storage.read_bytes("personas-bucket", "index.json"))
    ids = sorted(e["id"] for e in index["entries"])
    assert ids == ["aria-the-assistant", "blaine-the-bot"]


# --- seed_default_personas -------------------------------------------------


def _packaged_path(persona_id_: str) -> Path:
    return Path(personas_module.__file__).parent / "templates" / "personas" / f"{persona_id_}.md"


def test_seed_default_personas_first_run_writes_all():
    storage = FakeStorage()

    result = seed_default_personas(storage, bucket="personas-bucket")

    assert result == {"seeded": 2, "skipped": 0}

    index = json.loads(storage.read_bytes("personas-bucket", "index.json"))
    ids = {e["id"] for e in index["entries"]}
    assert ids == {"blank", "helpful-generalist"}
    for entry in index["entries"]:
        assert entry["builtin"] is True
        assert entry["team"] == "shared"
        assert entry["tags"] == []

    meta = storage.object_metadata("personas-bucket", "blank/persona.md")
    assert meta["metadata"] == {"team": "shared", "creator": "pnyx"}


def test_seed_default_personas_preserves_packaged_file_bytes():
    storage = FakeStorage()
    seed_default_personas(storage, bucket="personas-bucket")

    for persona_id_ in ("blank", "helpful-generalist"):
        written = storage.read_bytes("personas-bucket", f"{persona_id_}/persona.md")
        assert written == _packaged_path(persona_id_).read_bytes()
        # the agent-name placeholder must survive seeding untouched.
        assert b"{name}" in written


def test_seed_default_personas_second_run_all_skipped():
    storage = FakeStorage()
    seed_default_personas(storage, bucket="personas-bucket")

    result = seed_default_personas(storage, bucket="personas-bucket")

    assert result == {"seeded": 0, "skipped": 2}


def test_seed_default_personas_never_overwrites_existing_object():
    storage = FakeStorage()
    custom_body = b"<!-- description: hand-edited -->\ncustom body\n"
    storage.write_bytes(
        "personas-bucket",
        "blank/persona.md",
        custom_body,
        metadata={"team": "shared", "creator": "an-operator"},
        content_type="text/markdown",
    )

    seed_default_personas(storage, bucket="personas-bucket")

    assert storage.read_bytes("personas-bucket", "blank/persona.md") == custom_body


def test_seed_default_personas_merges_with_preexisting_room_authored_entries():
    """seed_default_personas must merge into index.json, not clobber it —
    a room-authored persona already in the index must survive a seed run."""
    storage = FakeStorage()
    create_persona(storage, bucket="personas-bucket", fields=_fields(), team="cs-team")

    seed_default_personas(storage, bucket="personas-bucket")

    index = json.loads(storage.read_bytes("personas-bucket", "index.json"))
    ids = {e["id"] for e in index["entries"]}
    assert ids == {"aria-the-assistant", "blank", "helpful-generalist"}
