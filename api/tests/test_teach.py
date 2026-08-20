"""Tests for kobo_api.teach (Task 1: "the teach route"): the two pure
formatting/naming functions behind "teach from chat" -- `format_note`
turns an operator's distilled chat note into the markdown pushed to
`bundle/intake/*`, `note_filename` derives that upload's filename. Neither
function touches git/kubectl at all (no fakes needed here); the route
tests in test_web_teach.py cover the HTTP layer that wires these into the
existing `kobo_api.intake.upload_intake_file` seam.
"""

from __future__ import annotations

from kobo_api.teach import format_note, note_filename

ON_DATE = "2026-08-19"


# --- format_note --------------------------------------------------------------


def test_format_note_includes_taught_in_chat_and_date():
    note = format_note("remember to be concise", taught_by=None, on_date=ON_DATE)
    assert "Taught in chat" in note
    assert ON_DATE in note


def test_format_note_without_user_omits_any_attribution_line():
    note = format_note("remember to be concise", taught_by=None, on_date=ON_DATE)
    assert "None" not in note
    assert "Taught by" not in note


def test_format_note_with_user_includes_the_email():
    note = format_note("remember to be concise", taught_by="ops@example.com", on_date=ON_DATE)
    assert "ops@example.com" in note


def test_format_note_body_is_the_operators_text_verbatim():
    text = "Line one.\nLine two, with   odd   spacing.\nEmoji too: \U0001f389"
    note = format_note(text, taught_by="ops@example.com", on_date=ON_DATE)
    assert text in note


def test_format_note_verbatim_body_preserves_an_em_dash_the_operator_typed():
    # "Verbatim" means verbatim: this codebase's own no-em-dash rule binds
    # kobo's OWN template copy, never a caller's own words -- stripping an
    # em dash out of the operator's text would violate the "body is the
    # operator's text verbatim" contract these note ARE memory content, not
    # UI chrome.
    text = "the room feels like a workshop — not a form"
    note = format_note(text, taught_by=None, on_date=ON_DATE)
    assert text in note


def test_format_note_own_template_copy_has_no_em_dash():
    note_no_user = format_note("hello", taught_by=None, on_date=ON_DATE)
    note_with_user = format_note("hello", taught_by="ops@example.com", on_date=ON_DATE)
    assert "—" not in note_no_user
    assert "—" not in note_with_user


def test_format_note_empty_text_still_produces_a_header():
    # format_note itself does no validation (that's the route's job, tested
    # in test_web_teach.py) -- it must still produce a sane, non-crashing
    # result for an edge-case caller.
    note = format_note("", taught_by=None, on_date=ON_DATE)
    assert "Taught in chat" in note


def test_format_note_different_users_produce_different_output():
    a = format_note("hello", taught_by="alice@example.com", on_date=ON_DATE)
    b = format_note("hello", taught_by="bob@example.com", on_date=ON_DATE)
    assert a != b
    assert "alice@example.com" in a
    assert "bob@example.com" not in a
    assert "bob@example.com" in b
    assert "alice@example.com" not in b


def test_format_note_different_dates_produce_different_output():
    a = format_note("hello", taught_by=None, on_date="2026-08-19")
    b = format_note("hello", taught_by=None, on_date="2026-01-01")
    assert "2026-08-19" in a
    assert "2026-08-19" not in b
    assert "2026-01-01" in b


# --- note_filename --------------------------------------------------------------


def test_note_filename_shape_is_chat_date_slug_md():
    filename = note_filename("Remember to be concise in replies", on_date=ON_DATE)
    assert filename.startswith("chat-20260819-")
    assert filename.endswith(".md")


def test_note_filename_slug_is_lowercase_hyphenated_words():
    filename = note_filename("Remember To Be Concise", on_date=ON_DATE)
    slug = filename.removeprefix("chat-20260819-").removesuffix(".md")
    assert slug == slug.lower()
    assert " " not in slug
    assert all(c.islower() or c.isdigit() or c == "-" for c in slug)


def test_note_filename_same_text_and_date_is_deterministic():
    a = note_filename("Remember to be concise", on_date=ON_DATE)
    b = note_filename("Remember to be concise", on_date=ON_DATE)
    assert a == b


def test_note_filename_different_text_yields_different_slug():
    a = note_filename("Remember to be concise", on_date=ON_DATE)
    b = note_filename("Always cite your sources", on_date=ON_DATE)
    assert a != b


def test_note_filename_bounded_length_even_for_a_long_message():
    long_text = " ".join(f"word{i}" for i in range(50))
    filename = note_filename(long_text, on_date=ON_DATE)
    # Same filename shape kobo_api.intake.INTAKE_FILENAME_RE enforces
    # (`^[a-z][a-z0-9-]{0,39}\.(md|txt)$`): the part before the extension
    # must be 40 characters or fewer.
    stem = filename.removesuffix(".md")
    assert len(stem) <= 40


def test_note_filename_matches_intake_filename_pattern_for_long_input():
    from kobo_api.intake import INTAKE_FILENAME_RE

    long_text = " ".join(f"word{i}" for i in range(50))
    filename = note_filename(long_text, on_date=ON_DATE)
    assert INTAKE_FILENAME_RE.fullmatch(filename)


def test_note_filename_matches_intake_filename_pattern_for_short_input():
    from kobo_api.intake import INTAKE_FILENAME_RE

    filename = note_filename("hi", on_date=ON_DATE)
    assert INTAKE_FILENAME_RE.fullmatch(filename)


def test_note_filename_emoji_only_text_falls_back_to_a_safe_slug():
    filename = note_filename("\U0001f389\U0001f973\U0001f38a", on_date=ON_DATE)
    from kobo_api.intake import INTAKE_FILENAME_RE

    assert INTAKE_FILENAME_RE.fullmatch(filename)


def test_note_filename_empty_text_falls_back_to_a_safe_slug():
    from kobo_api.intake import INTAKE_FILENAME_RE

    filename = note_filename("", on_date=ON_DATE)
    assert INTAKE_FILENAME_RE.fullmatch(filename)


def test_note_filename_whitespace_only_text_falls_back_to_a_safe_slug():
    from kobo_api.intake import INTAKE_FILENAME_RE

    filename = note_filename("   \n\t  ", on_date=ON_DATE)
    assert INTAKE_FILENAME_RE.fullmatch(filename)


def test_note_filename_punctuation_only_text_falls_back_to_a_safe_slug():
    from kobo_api.intake import INTAKE_FILENAME_RE

    filename = note_filename("!!! ??? ...", on_date=ON_DATE)
    assert INTAKE_FILENAME_RE.fullmatch(filename)


def test_note_filename_strips_non_slug_characters_but_keeps_words():
    filename = note_filename("Hello, world! It's #great.", on_date=ON_DATE)
    slug = filename.removeprefix("chat-20260819-").removesuffix(".md")
    assert "hello" in slug
    assert "world" in slug
    assert "," not in slug
    assert "!" not in slug
    assert "#" not in slug
    assert "'" not in slug
