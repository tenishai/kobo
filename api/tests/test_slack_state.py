from kobo_api.slack_state import (
    MAX_CHANNELS,
    SLACK_CHANNEL_RE,  # noqa: F401 (contract import: name must live here)
    clean_channels,
    decode_channels,
    derive_state,
    encode_channels,
)


def test_decode_none_is_empty():
    assert decode_channels(None) == []


def test_encode_decode_round_trip():
    channels = ["C0123456789", "C987654321A"]
    assert decode_channels(encode_channels(channels)) == channels


def test_clean_dedupes_preserving_first_occurrence_and_order():
    got = clean_channels(["C0123456789", "C987654321A", "C0123456789"])
    assert got == ["C0123456789", "C987654321A"]


def test_clean_rejects_bad_id_with_plain_reason():
    got = clean_channels(["notachannel"])
    assert isinstance(got, str)
    assert "channel ID" in got


def test_clean_rejects_over_cap():
    ids = [f"C00000000{i:02d}" for i in range(MAX_CHANNELS + 1)]
    assert clean_channels(ids) == "Eight channels at most for now."


def test_clean_rejects_empty_list():
    got = clean_channels([])
    assert isinstance(got, str)


def test_derive_state_matrix():
    assert derive_state([], wired=False) == "idle"
    assert derive_state(["C0123456789"], wired=False) == "waiting"
    assert derive_state(["C0123456789"], wired=True) == "connected"
    assert derive_state([], wired=True) == "waiting"
