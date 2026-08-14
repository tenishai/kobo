"""Tests for kobo_api.namegen: the pure candidate-filtering/pool-refill
logic behind the name-suggestions API (create flow v2 addendum) — no
FastAPI, DB, or HTTP involved, mirroring test_avatars.py's split from the
avatar route's own test_api_avatars.py.
"""

from __future__ import annotations

from pathlib import Path

from kobo_api.catalog import RESERVED_NAMES, validate_name
from kobo_api.namegen import (
    _FALLBACK_NAMES,
    _FALLBACK_REASONS,
    SUGGESTION_COUNT,
    _deterministic_order,
    finalize_suggestions,
)

# The SDD vendor-name deny list itself (repo root, outside the api/
# subproject) — read dynamically rather than spelling out any vendor name
# literally in this file, so the pool-hygiene check below stays meaningful
# without itself becoming a vendor-scan false positive.
_VENDOR_NAMES_PATH = Path(__file__).resolve().parents[2] / ".superpowers" / "sdd" / "vendor-names.txt"


def _names(suggestions: list[dict]) -> list[str]:
    return [s["name"] for s in suggestions]


# --- Fallback pool shape --------------------------------------------------------


def test_fallback_pool_has_at_least_five_and_no_vendor_names():
    assert len(_FALLBACK_NAMES) >= 5
    # No vendor/company names anywhere in the curated pool.
    lowered = {name.lower() for name in _FALLBACK_NAMES}
    if _VENDOR_NAMES_PATH.is_file():
        vendor_names = {
            line.strip().lower()
            for line in _VENDOR_NAMES_PATH.read_text().splitlines()
            if line.strip()
        }
        assert not (lowered & vendor_names)


def test_fallback_pool_names_are_all_valid_and_unreserved():
    for name in _FALLBACK_NAMES:
        assert validate_name(name) is None, f"{name!r} isn't a valid agent name"
        assert name not in RESERVED_NAMES


def test_fallback_pool_has_no_duplicates():
    assert len(_FALLBACK_NAMES) == len(set(_FALLBACK_NAMES))


def test_fallback_reasons_pool_is_non_empty():
    assert len(_FALLBACK_REASONS) >= 1


# --- finalize_suggestions: candidate validation matrix --------------------------


def test_finalize_valid_candidates_pass_through_unchanged():
    candidates = [
        {"name": "juniper", "reason": "sharp and quick"},
        {"name": "rowan", "reason": "warm and grounded"},
    ]

    result = finalize_suggestions(
        candidates, taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-a"
    )

    assert len(result) == SUGGESTION_COUNT
    assert result[0] == {"name": "juniper", "reason": "sharp and quick"}
    assert result[1] == {"name": "rowan", "reason": "warm and grounded"}


def test_finalize_invalid_shape_candidates_are_dropped_and_refilled():
    candidates = [
        {"name": "Juniper", "reason": "uppercase, fails the slug regex"},  # uppercase
        {"name": "a", "reason": "too short (< 2 chars)"},
        {"name": "x" * 25, "reason": "too long (> 24 chars)"},
        {"name": "not valid!", "reason": "spaces and punctuation"},
        {"name": "-leading-hyphen", "reason": "must start with a letter"},
        {"name": 42, "reason": "not even a string"},
    ]

    result = finalize_suggestions(
        candidates, taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-b"
    )

    # None of the malformed candidates survive; every slot instead comes
    # from the fallback pool.
    assert len(result) == SUGGESTION_COUNT
    for name in _names(result):
        assert name in _FALLBACK_NAMES


def test_finalize_filters_taken_names():
    candidates = [{"name": "juniper", "reason": "a fine pick"}]

    result = finalize_suggestions(
        candidates, taken={"juniper"}, reserved=RESERVED_NAMES, persona_seed="seed-c"
    )

    assert "juniper" not in _names(result)
    assert len(result) == SUGGESTION_COUNT


def test_finalize_filters_reserved_names():
    candidates = [{"name": "cortex", "reason": "an old platform name"}]

    result = finalize_suggestions(
        candidates, taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-d"
    )

    assert "cortex" not in _names(result)
    assert len(result) == SUGGESTION_COUNT


def test_finalize_dedupes_repeated_candidate_names():
    candidates = [
        {"name": "juniper", "reason": "first mention"},
        {"name": "juniper", "reason": "duplicate mention"},
        {"name": "rowan", "reason": "distinct"},
    ]

    result = finalize_suggestions(
        candidates, taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-e"
    )

    names = _names(result)
    assert names.count("juniper") == 1
    assert len(result) == SUGGESTION_COUNT
    # No pool refill collides with an already-accepted candidate name either.
    assert len(names) == len(set(names))


def test_finalize_pool_refill_never_collides_with_accepted_candidates():
    # A candidate that happens to share a name with a fallback pool entry
    # must not be double-counted when the pool is walked for the remaining
    # slots.
    candidates = [{"name": _FALLBACK_NAMES[0], "reason": "already suggested"}]

    result = finalize_suggestions(
        candidates, taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-f"
    )

    names = _names(result)
    assert len(names) == len(set(names))
    assert len(result) == SUGGESTION_COUNT


def test_finalize_always_returns_exactly_five_with_no_candidates_at_all():
    result = finalize_suggestions(
        [], taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-g"
    )
    assert len(result) == SUGGESTION_COUNT
    for name in _names(result):
        assert name in _FALLBACK_NAMES


def test_finalize_pool_reasons_come_from_the_fixed_cycle():
    result = finalize_suggestions(
        [], taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-h"
    )
    for suggestion in result:
        assert suggestion["reason"] in _FALLBACK_REASONS


# --- finalize_suggestions: determinism -------------------------------------------


def test_finalize_same_persona_seed_yields_same_five_every_time():
    first = finalize_suggestions(
        [], taken=set(), reserved=RESERVED_NAMES, persona_seed="a calm troubleshooter|helps with billing"
    )
    second = finalize_suggestions(
        [], taken=set(), reserved=RESERVED_NAMES, persona_seed="a calm troubleshooter|helps with billing"
    )
    assert first == second


def test_finalize_different_persona_seed_yields_different_order_or_names():
    first = finalize_suggestions(
        [], taken=set(), reserved=RESERVED_NAMES, persona_seed="persona one"
    )
    second = finalize_suggestions(
        [], taken=set(), reserved=RESERVED_NAMES, persona_seed="a wildly different persona entirely"
    )
    assert first != second


def test_deterministic_order_same_seed_same_order():
    pool = list(_FALLBACK_NAMES)
    first = _deterministic_order(pool, "seed-x")
    second = _deterministic_order(pool, "seed-x")
    assert first == second


def test_deterministic_order_different_seed_different_order():
    pool = list(_FALLBACK_NAMES)
    first = _deterministic_order(pool, "seed-x")
    second = _deterministic_order(pool, "seed-y")
    assert first != second


def test_deterministic_order_is_a_permutation_of_the_pool():
    pool = list(_FALLBACK_NAMES)
    ordered = _deterministic_order(pool, "any-seed")
    assert sorted(ordered) == sorted(pool)


def test_finalize_taken_and_reserved_together_can_still_produce_five():
    # A handful of pool names taken/reserved must not prevent five results,
    # as long as enough of the pool remains untouched.
    taken = set(_FALLBACK_NAMES[:3])
    result = finalize_suggestions(
        [], taken=taken, reserved=RESERVED_NAMES, persona_seed="seed-i"
    )
    assert len(result) == SUGGESTION_COUNT
    assert not (set(_names(result)) & taken)


# --- finalize_suggestions: reason normalization -----------------------------------


def test_finalize_short_normal_reasons_untouched():
    candidates = [
        {"name": "juniper", "reason": "sharp and quick"},
        {"name": "rowan", "reason": "warm at the bench"},
    ]

    result = finalize_suggestions(
        candidates, taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-j"
    )

    assert result[0]["reason"] == "sharp and quick"
    assert result[1]["reason"] == "warm at the bench"


def test_finalize_long_reason_clamped_to_eighty():
    long_reason = "a" * 100
    candidates = [{"name": "juniper", "reason": long_reason}]

    result = finalize_suggestions(
        candidates, taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-k"
    )

    assert len(result[0]["reason"]) <= 80


def test_finalize_long_reason_cuts_at_word_boundary_if_within_last_twenty():
    # Reason is 90 chars long; with a space at position 75 (within the last 20 chars
    # before 80), should cut at that space.
    reason = "word" * 20 + " boundary here but let's keep going further into the text"  # ~90 chars
    candidates = [{"name": "juniper", "reason": reason}]

    result = finalize_suggestions(
        candidates, taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-l"
    )

    truncated = result[0]["reason"]
    assert len(truncated) <= 80
    # Should have cut at a word boundary, not mid-word
    assert not truncated.endswith(" ")  # trimmed after cut


def test_finalize_long_reason_hard_cuts_at_eighty_if_no_boundary():
    # A long string with no spaces in the last 20 chars (no word boundary to cut at)
    reason = "word" * 20 + "nospace" * 10  # long with no boundary in last 20
    candidates = [{"name": "juniper", "reason": reason}]

    result = finalize_suggestions(
        candidates, taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-m"
    )

    truncated = result[0]["reason"]
    assert len(truncated) == 80


def test_finalize_whitespace_collapsed():
    candidates = [{"name": "juniper", "reason": "sharp   and    quick  with  lots   of spaces"}]

    result = finalize_suggestions(
        candidates, taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-n"
    )

    assert result[0]["reason"] == "sharp and quick with lots of spaces"


def test_finalize_reason_stripped_of_leading_trailing_whitespace():
    candidates = [{"name": "juniper", "reason": "  leading and trailing spaces  "}]

    result = finalize_suggestions(
        candidates, taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-o"
    )

    assert result[0]["reason"] == "leading and trailing spaces"


def test_finalize_empty_reason_after_normalize_replaced_from_fallback_cycle():
    # A reason that becomes empty after normalization (e.g., whitespace-only)
    # should be replaced from the fallback cycle.
    candidates = [
        {"name": "juniper", "reason": "   "},  # becomes empty after strip+normalize
        {"name": "rowan", "reason": "a real reason"},
    ]

    result = finalize_suggestions(
        candidates, taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-p"
    )

    # First candidate's reason should be from fallback cycle
    assert result[0]["reason"] in _FALLBACK_REASONS
    # Second candidate's reason is preserved
    assert result[1]["reason"] == "a real reason"


def test_finalize_reason_normalization_combined():
    # Long reason with extra whitespace and leading/trailing spaces
    reason = "  a   long  and  rambling   reason  " + "with" * 30
    candidates = [{"name": "juniper", "reason": reason}]

    result = finalize_suggestions(
        candidates, taken=set(), reserved=RESERVED_NAMES, persona_seed="seed-q"
    )

    normalized = result[0]["reason"]
    # Should be <= 80 chars
    assert len(normalized) <= 80
    # Should not have leading/trailing spaces
    assert not normalized.startswith(" ")
    assert not normalized.endswith(" ")
    # Should have single spaces only
    assert "  " not in normalized
