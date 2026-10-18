"""Tests for kobo_api.persona_draft: the pure per-field validation/fallback
logic behind the persona-draft API (Persona v3 addendum) — no FastAPI, DB,
or HTTP involved, mirroring test_namegen.py's split from the
name-suggestions route's own test_api_names.py.
"""

from __future__ import annotations

import pytest

from kobo_api.persona_draft import (
    _fallback_distilled_name,
    _fallback_distilled_who,
    _fallback_name,
    _fallback_tags,
    _fallback_what,
    _fallback_who,
    _first_clause,
    _shorten_name,
    finalize_distill,
    finalize_draft,
)
from kobo_api.personas import PersonaFields, persona_id

JOB = "Help customers troubleshoot billing issues quickly and politely."


def _assert_persona_valid(draft: dict) -> None:
    """A drafted dict must always construct a real PersonaFields — the
    "always drafts SOMETHING editable" contract, checked against the exact
    model the create-persona route validates against, not a re-derived
    bound."""
    PersonaFields(**draft)


# --- _fallback_name -----------------------------------------------------------


def test_fallback_name_slugifies_leading_words():
    assert _fallback_name("Help customers troubleshoot billing issues.") == (
        "help-customers"
    )


def test_fallback_name_caps_at_two_words():
    # Short-names ruling: a persona name is one or two words.
    assert _fallback_name("one two three four five six seven") == "one-two"


# --- _shorten_name (short-names ruling) --------------------------------------


def test_shorten_name_leaves_a_short_name_alone():
    assert _shorten_name("billing-buddy") == "billing-buddy"
    assert _shorten_name("Ada (Release)") == "Ada (Release)"
    assert _shorten_name("ledger") == "ledger"


def test_shorten_name_keeps_the_last_two_words_of_a_long_one():
    # The tail is the noun; the head is the trait list that belongs in
    # who_they_are.
    assert _shorten_name("code-grounded-player-intel") == "player-intel"
    assert _shorten_name("direct factual condensed mellow") == "condensed-mellow"


def test_finalize_draft_shortens_a_long_model_name():
    raw = {
        "name": "terse-skeptical-release-auditor",
        "who_they_are": "a skeptical auditor who cites the clause",
        "what_they_do": "Audit releases.",
    }
    result = finalize_draft(raw, job="Audit releases")
    assert result["name"] == "release-auditor"


def test_fallback_name_no_alnum_words_uses_default():
    assert _fallback_name("!!! ??? ...") == "new-agent"


def test_fallback_name_never_exceeds_persona_fields_bound():
    long_job = "supercalifragilisticexpialidocious " * 10
    name = _fallback_name(long_job)
    assert len(name) <= 60


def test_fallback_name_digit_led_job_passes_persona_id():
    # "3D print parts" slugifies to "3d-print-parts", which starts with a
    # digit and fails persona_id. _fallback_name must ensure the result
    # passes persona_id by prefixing "the-" when needed.
    name = _fallback_name("3D print parts")
    persona_id(name)  # Should not raise PersonaError


# --- _first_clause / _fallback_who --------------------------------------------


def test_first_clause_stops_at_period():
    assert _first_clause("Handle billing questions. Escalate refunds.") == (
        "Handle billing questions"
    )


def test_first_clause_whole_text_when_no_boundary():
    assert _first_clause("just do the thing") == "just do the thing"


def test_first_clause_stops_at_a_newline_too():
    # A newline is itself a clause boundary (who_they_are must end up a
    # single line one way or another) — the text after it is dropped, same
    # as text after a period.
    assert _first_clause("first line\nsecond line. more") == "first line"


def test_first_clause_falls_back_to_whole_job_when_clause_is_empty():
    # job starts with a clause boundary itself -> the "first clause" slice
    # is empty; falling back to the whole (collapsed) text keeps the
    # who-line meaningful rather than blank.
    assert _first_clause(". lead with something useful") == "lead with something useful"


def test_fallback_who_uses_template_and_first_clause():
    assert _fallback_who("Help with billing. And more.") == (
        "an agent whose whole job is Help with billing"
    )


def test_fallback_who_never_exceeds_persona_fields_bound():
    long_job = "x" * 490  # no clause boundary at all -> whole text is the clause
    who = _fallback_who(long_job)
    assert len(who) <= 200


def test_fallback_who_all_punctuation_returns_fallback_text():
    # When job is all punctuation, _first_clause returns empty string,
    # which would create a dangling "an agent whose whole job is ".
    # _fallback_who must return "an agent with one clear job" instead.
    who = _fallback_who("!!!")
    assert who == "an agent with one clear job"


# --- _fallback_what ------------------------------------------------------------


def test_fallback_what_is_job_verbatim():
    assert _fallback_what(JOB) == JOB


def test_fallback_what_clamped_to_persona_fields_bound():
    long_job = "a" * 12600
    assert len(_fallback_what(long_job)) <= 12000


# --- finalize_draft: all-valid passthrough ------------------------------------


def test_finalize_draft_all_valid_fields_pass_through_unchanged():
    raw = {
        "name": "billing-buddy",
        "who_they_are": "a calm troubleshooter for billing questions",
        "what_they_do": "Helps customers resolve billing issues quickly.",
        "purpose_tags": ["billing", "support"],
    }

    result = finalize_draft(raw, job=JOB)

    assert result == raw


# --- finalize_draft: full deterministic fallback ------------------------------


def test_finalize_draft_none_raw_falls_back_on_every_field():
    result = finalize_draft(None, job=JOB)

    assert result["name"] == _fallback_name(JOB)
    assert result["who_they_are"] == _fallback_who(JOB)
    assert result["what_they_do"] == _fallback_what(JOB)
    assert result["purpose_tags"] == _fallback_tags(
        _fallback_name(JOB), _fallback_who(JOB), _fallback_what(JOB)
    )


def test_finalize_draft_non_dict_raw_treated_as_unusable():
    result = finalize_draft("not a dict", job=JOB)  # type: ignore[arg-type]

    assert result["name"] == _fallback_name(JOB)


def test_finalize_draft_always_persona_fields_valid_on_full_fallback():
    _assert_persona_valid(finalize_draft(None, job=JOB))


# --- finalize_draft: per-field fallback matrix --------------------------------


def test_finalize_draft_bad_name_falls_back_alone():
    raw = {
        "name": "x" * 61,  # over PersonaFields.name's 60-char bound
        "who_they_are": "a calm troubleshooter for billing questions",
        "what_they_do": "Helps customers resolve billing issues quickly.",
    }

    result = finalize_draft(raw, job=JOB)

    assert result["name"] == _fallback_name(JOB)
    assert result["who_they_are"] == raw["who_they_are"]
    assert result["what_they_do"] == raw["what_they_do"]


def test_finalize_draft_digit_led_name_falls_back():
    # "3D print parts" passes PersonaFields validation (it's 14 chars),
    # but persona_id() rejects it because it starts with a digit after
    # slugification. finalize_draft must fall back.
    raw = {
        "name": "3D print parts",
        "who_they_are": "a calm troubleshooter for billing questions",
        "what_they_do": "Helps customers resolve billing issues quickly.",
    }

    result = finalize_draft(raw, job="Handle parts printing")

    # The fallback name must pass persona_id
    persona_id(result["name"])
    assert result["who_they_are"] == raw["who_they_are"]
    assert result["what_they_do"] == raw["what_they_do"]


def test_finalize_draft_bad_who_they_are_falls_back_alone():
    raw = {
        "name": "billing-buddy",
        "who_they_are": "x" * 201,  # over PersonaFields.who_they_are's 200-char bound
        "what_they_do": "Helps customers resolve billing issues quickly.",
    }

    result = finalize_draft(raw, job=JOB)

    assert result["name"] == raw["name"]
    assert result["who_they_are"] == _fallback_who(JOB)
    assert result["what_they_do"] == raw["what_they_do"]


def test_finalize_draft_who_they_are_with_newline_falls_back():
    # PersonaFields.who_they_are must be a single line — a multi-line
    # generator reply for this field is invalid even within the 200-char
    # bound.
    raw = {
        "name": "billing-buddy",
        "who_they_are": "first line\nsecond line",
        "what_they_do": "Helps customers resolve billing issues quickly.",
    }

    result = finalize_draft(raw, job=JOB)

    assert result["who_they_are"] == _fallback_who(JOB)


def test_finalize_draft_bad_what_they_do_falls_back_alone():
    raw = {
        "name": "billing-buddy",
        "who_they_are": "a calm troubleshooter for billing questions",
        "what_they_do": "x" * 12001,  # over the draft-acceptance bound (12000)
    }

    result = finalize_draft(raw, job=JOB)

    assert result["name"] == raw["name"]
    assert result["who_they_are"] == raw["who_they_are"]
    assert result["what_they_do"] == _fallback_what(JOB)


def test_finalize_draft_strips_surrounding_whitespace_from_valid_fields():
    raw = {
        "name": "  billing-buddy  ",
        "who_they_are": "a calm troubleshooter for billing questions",
        "what_they_do": "Helps customers resolve billing issues quickly.",
    }

    result = finalize_draft(raw, job=JOB)

    assert result["name"] == "billing-buddy"


@pytest.mark.parametrize(
    "job",
    [
        "Handle refunds and billing disputes for premium customers.",
        "Write weekly release notes from the merged PR list.",
        "Keep the on-call runbook up to date after every incident.",
    ],
)
def test_finalize_draft_full_fallback_is_deterministic_for_same_job(job):
    first = finalize_draft(None, job=job)
    second = finalize_draft(None, job=job)
    assert first == second


# --- finalize_draft: purpose_tags (persona-metadata plan, Task 2) --------------


NAME = "billing-buddy"
WHO = "a calm troubleshooter for billing questions"
WHAT = "Helps customers resolve billing issues quickly."


def test_finalize_draft_model_purpose_tags_normalized():
    raw = {
        "name": NAME,
        "who_they_are": WHO,
        "what_they_do": WHAT,
        "purpose_tags": ["Billing", "billing", " Support ", "REFUNDS"],
    }

    result = finalize_draft(raw, job=JOB)

    assert result["purpose_tags"] == ["billing", "support", "refunds"]


def test_finalize_draft_model_purpose_tags_capped_at_five():
    raw = {
        "name": NAME,
        "who_they_are": WHO,
        "what_they_do": WHAT,
        "purpose_tags": ["one", "two", "three", "four", "five", "six", "seven"],
    }

    result = finalize_draft(raw, job=JOB)

    assert result["purpose_tags"] == ["one", "two", "three", "four", "five"]


def test_finalize_draft_missing_purpose_tags_falls_back_to_persona_own_tags():
    raw = {"name": NAME, "who_they_are": WHO, "what_they_do": WHAT}

    result = finalize_draft(raw, job=JOB)

    assert result["purpose_tags"] == _fallback_tags(NAME, WHO, WHAT)


def test_finalize_draft_purpose_tags_fallback_uses_finalized_fields_not_job():
    """The fallback tier is "the persona's own tags" — derived from the
    FINALIZED name/who_they_are/what_they_do (whichever tier those
    themselves came from), never re-derived from the raw job text
    directly. Here the model supplies a valid name/who/what but no tags at
    all, so the fallback must reflect those model-supplied fields, not
    `_fallback_name(job)`/`_fallback_who(job)`/`_fallback_what(job)`."""
    raw = {"name": NAME, "who_they_are": WHO, "what_they_do": WHAT}

    result = finalize_draft(raw, job="Do something totally unrelated to billing.")

    assert result["purpose_tags"] == _fallback_tags(NAME, WHO, WHAT)


# --- _fallback_tags -------------------------------------------------------------


def test_fallback_tags_never_exceeds_five():
    tags = _fallback_tags(
        "word-mill",
        "turns every incoming word into something useful for the team",
        "Processes words, phrases, sentences, paragraphs, and entire documents.",
    )
    assert len(tags) <= 5


def test_fallback_tags_deterministic_for_same_fields():
    first = _fallback_tags(NAME, WHO, WHAT)
    second = _fallback_tags(NAME, WHO, WHAT)
    assert first == second


# --- distill (persona-first station addendum): fallback + finalize -----------
#
# `finalize_distill` is finalize_draft's counterpart for a ready-made persona
# DOCUMENT rather than a one-line job description: it produces discovery
# metadata (name/who_they_are/purpose_tags, no what_they_do - the document
# itself already IS the persona body) from `body`, falling each field back
# individually on the same "never block the station" contract.


DISTILL_BODY = "# Billing Buddy\n\nA calm troubleshooter for billing questions.\n"


# --- _fallback_distilled_name --------------------------------------------------


def test_fallback_distilled_name_uses_first_markdown_heading():
    assert _fallback_distilled_name(DISTILL_BODY) == "billing-buddy"


def test_fallback_distilled_name_falls_back_to_first_two_words_when_no_heading():
    body = "A calm troubleshooter for billing questions, no heading here."
    assert _fallback_distilled_name(body) == "a-calm"


def test_fallback_distilled_name_no_alnum_words_uses_default():
    assert _fallback_distilled_name("!!! ??? ...") == "new-agent"


def test_fallback_distilled_name_never_exceeds_persona_fields_bound():
    body = "# " + ("supercalifragilisticexpialidocious " * 10)
    name = _fallback_distilled_name(body)
    assert len(name) <= 60


def test_fallback_distilled_name_digit_led_heading_gets_the_prefix():
    # "3D print parts" slugifies to "3d-print-parts", which starts with a
    # digit and fails persona_id - the same "the-" prefix rescue
    # _fallback_name's own digit-led case uses.
    body = "# 3D print parts\n\nBody text."
    name = _fallback_distilled_name(body)
    persona_id(name)  # must not raise


def test_fallback_distilled_name_ignores_a_bare_hash_with_no_heading_text():
    # A line that's just "#" (no text after the marker) isn't a usable
    # heading at all - falls through to the first-two-words tier instead.
    body = "#\nA calm troubleshooter for billing questions."
    assert _fallback_distilled_name(body) == "a-calm"


def test_fallback_distilled_name_uses_the_first_heading_anywhere_in_the_body():
    # "the first markdown heading" means the first one found anywhere in
    # the document, not just on line 1 - a preamble line before it is fine.
    body = "Some preamble line.\n\n## Billing Buddy\n\nMore text."
    assert _fallback_distilled_name(body) == "billing-buddy"


# --- _fallback_distilled_who ----------------------------------------------------


def test_fallback_distilled_who_uses_first_non_heading_line():
    assert (
        _fallback_distilled_who(DISTILL_BODY)
        == "A calm troubleshooter for billing questions."
    )


def test_fallback_distilled_who_skips_multiple_headings():
    body = "# Billing Buddy\n## A subheading\nThe real first line of prose."
    assert _fallback_distilled_who(body) == "The real first line of prose."


def test_fallback_distilled_who_skips_blank_lines():
    body = "\n\n   \nActual content here."
    assert _fallback_distilled_who(body) == "Actual content here."


def test_fallback_distilled_who_truncates_to_two_hundred_chars():
    long_line = "x" * 250
    body = f"# Heading\n{long_line}"
    who = _fallback_distilled_who(body)
    assert len(who) <= 200


def test_fallback_distilled_who_all_headings_or_blank_returns_fallback_text():
    body = "# Just a heading\n\n## Another heading\n\n"
    assert _fallback_distilled_who(body) == "an agent with one clear job"


# --- finalize_distill: shape (no what_they_do) ---------------------------------


def test_finalize_distill_result_has_no_what_they_do_key():
    result = finalize_distill(None, body=DISTILL_BODY)
    assert set(result.keys()) == {"name", "who_they_are", "purpose_tags"}


# --- finalize_distill: all-valid passthrough ------------------------------------


def test_finalize_distill_all_valid_fields_pass_through_unchanged():
    raw = {
        "name": "billing-buddy",
        "who_they_are": "a calm troubleshooter for billing questions",
        "purpose_tags": ["billing", "support"],
    }

    result = finalize_distill(raw, body=DISTILL_BODY)

    assert result == raw


# --- finalize_distill: full deterministic fallback ------------------------------


def test_finalize_distill_none_raw_falls_back_on_every_field():
    result = finalize_distill(None, body=DISTILL_BODY)

    assert result["name"] == _fallback_distilled_name(DISTILL_BODY)
    assert result["who_they_are"] == _fallback_distilled_who(DISTILL_BODY)
    assert result["purpose_tags"] == []


def test_finalize_distill_non_dict_raw_treated_as_unusable():
    result = finalize_distill("not a dict", body=DISTILL_BODY)  # type: ignore[arg-type]

    assert result["name"] == _fallback_distilled_name(DISTILL_BODY)


@pytest.mark.parametrize(
    "body",
    [
        "# Refund Runner\n\nHandles refunds for premium customers.",
        "Write weekly release notes from the merged PR list.",
        "## On-call Helper\nKeeps the on-call runbook up to date.",
    ],
)
def test_finalize_distill_full_fallback_is_deterministic_for_same_body(body):
    first = finalize_distill(None, body=body)
    second = finalize_distill(None, body=body)
    assert first == second


# --- finalize_distill: per-field fallback matrix --------------------------------


def test_finalize_distill_bad_name_falls_back_alone():
    raw = {
        "name": "x" * 61,  # over PersonaFields.name's 60-char bound
        "who_they_are": "a calm troubleshooter for billing questions",
    }

    result = finalize_distill(raw, body=DISTILL_BODY)

    assert result["name"] == _fallback_distilled_name(DISTILL_BODY)
    assert result["who_they_are"] == raw["who_they_are"]


def test_finalize_distill_digit_led_name_falls_back():
    raw = {"name": "3D print parts", "who_they_are": "an always-on system monitor"}

    result = finalize_distill(raw, body="Monitor systems 24/7 without ever sleeping.")

    persona_id(result["name"])  # must not raise
    assert result["who_they_are"] == raw["who_they_are"]


def test_finalize_distill_who_they_are_with_newline_falls_back():
    raw = {"name": "billing-buddy", "who_they_are": "first line\nsecond line"}

    result = finalize_distill(raw, body=DISTILL_BODY)

    assert result["who_they_are"] == _fallback_distilled_who(DISTILL_BODY)


# --- finalize_distill: purpose_tags ---------------------------------------------


def test_finalize_distill_model_purpose_tags_normalized():
    raw = {
        "name": "billing-buddy",
        "who_they_are": "a calm troubleshooter",
        "purpose_tags": ["Billing", "billing", " Support ", "REFUNDS"],
    }

    result = finalize_distill(raw, body=DISTILL_BODY)

    assert result["purpose_tags"] == ["billing", "support", "refunds"]


def test_finalize_distill_model_purpose_tags_capped_at_five():
    raw = {
        "name": "billing-buddy",
        "who_they_are": "a calm troubleshooter",
        "purpose_tags": ["one", "two", "three", "four", "five", "six", "seven"],
    }

    result = finalize_distill(raw, body=DISTILL_BODY)

    assert result["purpose_tags"] == ["one", "two", "three", "four", "five"]


def test_finalize_distill_missing_purpose_tags_is_empty_list_not_derived():
    """Unlike finalize_draft's fallback tier (the persona's own derived
    tags, via _fallback_tags), a distill has no what_they_do to derive tags
    from at all - the fallback here is a plain empty list, full stop."""
    raw = {"name": "billing-buddy", "who_they_are": "a calm troubleshooter"}

    result = finalize_distill(raw, body=DISTILL_BODY)

    assert result["purpose_tags"] == []


def test_fallback_tags_are_honestly_empty():
    """Create-flow polish (2026-08-28): the noun-derivation tier is retired
    -- it produced tags like "embarrassment" at scale. No model tags and no
    human tags means NO tags."""
    assert _fallback_tags(NAME, WHO, WHAT) == []
