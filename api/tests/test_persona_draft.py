"""Tests for kobo_api.persona_draft: the pure per-field validation/fallback
logic behind the persona-draft API (Persona v3 addendum) — no FastAPI, DB,
or HTTP involved, mirroring test_namegen.py's split from the
name-suggestions route's own test_api_names.py.
"""

from __future__ import annotations

import pytest

from kobo_api.persona_draft import (
    _fallback_name,
    _fallback_tags,
    _fallback_what,
    _fallback_who,
    _first_clause,
    finalize_draft,
)
from kobo_api.personas import PersonaFields, persona_id
from kobo_api.purpose_tags import normalize_tags

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
        "help-customers-troubleshoot-billing-issues"
    )


def test_fallback_name_caps_at_five_words():
    assert _fallback_name("one two three four five six seven") == (
        "one-two-three-four-five"
    )


def test_fallback_name_lowercases():
    assert _fallback_name("HELLO World") == "hello-world"


def test_fallback_name_no_alnum_words_uses_default():
    assert _fallback_name("!!! ??? ...") == "new-agent"


def test_fallback_name_never_exceeds_persona_fields_bound():
    long_job = "supercalifragilisticexpialidocious " * 10
    name = _fallback_name(long_job)
    assert len(name) <= 60


def test_fallback_name_is_persona_fields_valid():
    _assert_persona_valid(
        {
            "name": _fallback_name(JOB),
            "who_they_are": _fallback_who(JOB),
            "what_they_do": _fallback_what(JOB),
        }
    )


def test_fallback_name_digit_led_job_passes_persona_id():
    # "3D print parts" slugifies to "3d-print-parts", which starts with a
    # digit and fails persona_id. _fallback_name must ensure the result
    # passes persona_id by prefixing "the-" when needed.
    name = _fallback_name("3D print parts")
    persona_id(name)  # Should not raise PersonaError


def test_fallback_name_digit_led_job_example_2():
    # "24/7 monitor pagers" slugifies to "24-7-monitor-pagers", which starts
    # with a digit and fails persona_id. _fallback_name must ensure the
    # result passes persona_id.
    name = _fallback_name("24/7 monitor pagers")
    persona_id(name)  # Should not raise PersonaError


def test_fallback_name_with_the_prefix_is_under_limit():
    # When we prefix "the-" to a 5-word slug, the result must still fit
    # within PersonaFields.name's 60-char limit.
    name = _fallback_name("3D print parts")
    assert len(name) <= 60


# --- _first_clause / _fallback_who --------------------------------------------


def test_first_clause_stops_at_period():
    assert _first_clause("Handle billing questions. Escalate refunds.") == (
        "Handle billing questions"
    )


def test_first_clause_stops_at_semicolon_or_bang_or_question():
    assert _first_clause("Do the thing; then stop.") == "Do the thing"
    assert _first_clause("Do it now! Then relax.") == "Do it now"
    assert _first_clause("Can you help? Yes.") == "Can you help"


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


def test_fallback_who_has_no_newline():
    who = _fallback_who("first line\nsecond line")
    assert "\n" not in who


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
    long_job = "a" * 600
    assert len(_fallback_what(long_job)) <= 500


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


def test_finalize_draft_empty_dict_falls_back_on_every_field():
    result = finalize_draft({}, job=JOB)

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


def test_finalize_draft_digit_led_name_example_2():
    # "24/7 monitor pagers" should also fall back and be persona_id-valid.
    raw = {
        "name": "24/7 monitor pagers",
        "who_they_are": "an always-on system monitor",
        "what_they_do": "Monitor all pagers and alerts.",
    }

    result = finalize_draft(raw, job="Monitor systems 24/7")

    # The fallback name must pass persona_id
    persona_id(result["name"])
    assert result["who_they_are"] == raw["who_they_are"]


def test_finalize_draft_missing_name_falls_back_alone():
    raw = {
        "who_they_are": "a calm troubleshooter for billing questions",
        "what_they_do": "Helps customers resolve billing issues quickly.",
    }

    result = finalize_draft(raw, job=JOB)

    assert result["name"] == _fallback_name(JOB)
    assert result["who_they_are"] == raw["who_they_are"]


def test_finalize_draft_non_string_name_falls_back_alone():
    raw = {
        "name": 12345,
        "who_they_are": "a calm troubleshooter for billing questions",
        "what_they_do": "Helps customers resolve billing issues quickly.",
    }

    result = finalize_draft(raw, job=JOB)

    assert result["name"] == _fallback_name(JOB)


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
        "what_they_do": "x" * 501,  # over PersonaFields.what_they_do's 500-char bound
    }

    result = finalize_draft(raw, job=JOB)

    assert result["name"] == raw["name"]
    assert result["who_they_are"] == raw["who_they_are"]
    assert result["what_they_do"] == _fallback_what(JOB)


def test_finalize_draft_empty_string_field_falls_back():
    raw = {
        "name": "",
        "who_they_are": "a calm troubleshooter for billing questions",
        "what_they_do": "Helps customers resolve billing issues quickly.",
    }

    result = finalize_draft(raw, job=JOB)

    assert result["name"] == _fallback_name(JOB)


def test_finalize_draft_whitespace_only_field_falls_back():
    raw = {
        "name": "   ",
        "who_they_are": "a calm troubleshooter for billing questions",
        "what_they_do": "Helps customers resolve billing issues quickly.",
    }

    result = finalize_draft(raw, job=JOB)

    assert result["name"] == _fallback_name(JOB)


def test_finalize_draft_strips_surrounding_whitespace_from_valid_fields():
    raw = {
        "name": "  billing-buddy  ",
        "who_they_are": "a calm troubleshooter for billing questions",
        "what_they_do": "Helps customers resolve billing issues quickly.",
    }

    result = finalize_draft(raw, job=JOB)

    assert result["name"] == "billing-buddy"


def test_finalize_draft_all_fields_bad_equals_full_fallback():
    raw = {"name": 1, "who_they_are": None, "what_they_do": ["not", "a", "string"]}

    result = finalize_draft(raw, job=JOB)

    assert result == finalize_draft(None, job=JOB)


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


def test_finalize_draft_non_list_purpose_tags_falls_back():
    raw = {
        "name": NAME,
        "who_they_are": WHO,
        "what_they_do": WHAT,
        "purpose_tags": "billing, refunds",
    }

    result = finalize_draft(raw, job=JOB)

    assert result["purpose_tags"] == _fallback_tags(NAME, WHO, WHAT)


def test_finalize_draft_empty_list_purpose_tags_falls_back():
    raw = {"name": NAME, "who_they_are": WHO, "what_they_do": WHAT, "purpose_tags": []}

    result = finalize_draft(raw, job=JOB)

    assert result["purpose_tags"] == _fallback_tags(NAME, WHO, WHAT)


def test_finalize_draft_all_non_string_purpose_tags_falls_back():
    raw = {
        "name": NAME,
        "who_they_are": WHO,
        "what_they_do": WHAT,
        "purpose_tags": [1, None, ["nested"]],
    }

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


def test_fallback_tags_derives_from_persona_own_fields_and_caps_at_five():
    # Manually traced against personas._derive_tags' own algorithm (every
    # lowercase word from name+who+what, minus stopwords, deduped,
    # longest-first, stable on ties): "a"/"for" are stopwords; the
    # remaining words rank troubleshooter(14), questions/customers(9,
    # first-seen order), billing/resolve/quickly(7), issues(6),
    # buddy/helps(5), calm(4) — capped at 5 here (the draft's own cap,
    # narrower than _derive_tags' own cap of 6).
    tags = _fallback_tags(NAME, WHO, WHAT)

    assert tags == ["troubleshooter", "questions", "customers", "billing", "resolve"]


def test_fallback_tags_matches_derive_tags_normalized_and_capped():
    from kobo_api.personas import _derive_tags

    fields = PersonaFields(name=NAME, who_they_are=WHO, what_they_do=WHAT)
    expected = normalize_tags(_derive_tags(fields, persona_id(NAME)))[:5]

    assert _fallback_tags(NAME, WHO, WHAT) == expected


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
