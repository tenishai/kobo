from kobo_api.purpose_tags import (
    MAX_CREATE_TAGS,
    clean_tags,
    decode_tags,
    encode_tags,
    normalize_tags,
)


def test_decode_none_is_empty():
    assert decode_tags(None) == []


def test_decode_empty_string_is_empty():
    assert decode_tags("") == []


def test_encode_decode_round_trip():
    tags = ["billing", "refunds"]
    assert decode_tags(encode_tags(tags)) == tags


def test_normalize_lowercases():
    assert normalize_tags(["Billing", "REFUNDS"]) == ["billing", "refunds"]


def test_normalize_strips_whitespace():
    assert normalize_tags([" billing ", "refunds\t"]) == ["billing", "refunds"]


def test_normalize_dedupes_preserving_first_occurrence_and_order():
    assert normalize_tags(["billing", "refunds", "billing"]) == ["billing", "refunds"]


def test_normalize_hyphenates_internal_whitespace():
    # A multi-word tag becomes one hyphenated slug. The catalog index stores
    # tags space-joined and splits them back on read (kobo_api.search), so a
    # tag with a space in it would otherwise shatter into word-shards there --
    # the live persona audit-check carried "cloud security" and "security
    # regulations", whose shards collided on "security" and blew up the
    # Personas page's tag-keyed each (Svelte each_key_duplicate, 2026-08-28).
    assert normalize_tags(["cloud security", "security\tregulations"]) == [
        "cloud-security",
        "security-regulations",
    ]


def test_normalize_dedupes_after_hyphenation():
    assert normalize_tags(["cloud security", "cloud-security"]) == ["cloud-security"]


def test_normalize_dedupes_after_case_and_whitespace_folding():
    assert normalize_tags(["Billing", " billing", "BILLING "]) == ["billing"]


def test_normalize_drops_empty_and_whitespace_only_entries():
    assert normalize_tags(["billing", "", "   "]) == ["billing"]


def test_normalize_drops_non_string_entries():
    assert normalize_tags(["billing", 1, None, ["nested"]]) == ["billing"]


def test_normalize_non_list_input_is_empty():
    assert normalize_tags("billing") == []
    assert normalize_tags(None) == []


def test_normalize_empty_list_is_empty():
    assert normalize_tags([]) == []


def test_normalize_applies_no_cap():
    tags = [f"tag{i}" for i in range(20)]
    assert normalize_tags(tags) == tags


def test_clean_dedupes_preserving_first_occurrence_and_order():
    got = clean_tags(["billing", "refunds", "billing"])
    assert got == ["billing", "refunds"]


def test_clean_rejects_over_cap():
    tags = [f"tag{i}" for i in range(MAX_CREATE_TAGS + 1)]
    assert clean_tags(tags) == "Eight tags at most."


def test_clean_cap_checked_after_dedupe():
    # MAX_CREATE_TAGS+1 repeats of the SAME tag de-dupe down to one —
    # the cap must not trip on the raw (pre-dedupe) count.
    tags = ["billing"] * (MAX_CREATE_TAGS + 1)
    assert clean_tags(tags) == ["billing"]


def test_clean_exactly_at_cap_is_accepted():
    tags = [f"tag{i}" for i in range(MAX_CREATE_TAGS)]
    assert clean_tags(tags) == tags


def test_clean_empty_list_is_accepted_as_empty():
    # Unlike Slack channels (where an empty list is a rejected
    # disconnect-shape), purpose_tags is optional metadata — an empty list
    # is just "no tags", not an error.
    assert clean_tags([]) == []
