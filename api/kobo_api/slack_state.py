"""Channel-list plumbing for the two-lane Slack flow.

Pure helpers only: JSON encode/decode for the Agent column, validation
with plain copy, and the little state machine the payload reports. The
routes own persistence; the UI owns rendering; this module owns truth.
"""
import json
import re

# Single source for channel-ID shape; main.py imports it from here.
# Slack has migrated private channels to C ids, so C-prefixed is enough.
SLACK_CHANNEL_RE = re.compile(r"^C[A-Z0-9]{6,}$")

MAX_CHANNELS = 8
_CAP_MESSAGE = "Eight channels at most for now."


def decode_channels(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [str(entry) for entry in json.loads(raw)]


def encode_channels(channels: list[str]) -> str:
    return json.dumps(channels)


def clean_channels(channels: list[str]) -> list[str] | str:
    """The deduped, order-preserving list, or a plain error string.

    Three error triggers, each its own plain-copy return: an empty input
    list ("Add at least one channel."), any entry that doesn't match
    `SLACK_CHANNEL_RE` once stripped ("that doesn't look like a channel ID
    (it starts with C)"), or a deduped count over `MAX_CHANNELS` (the cap
    is checked AFTER dedupe, so repeats of the same id never trip it) —
    `_CAP_MESSAGE`.
    """
    if not channels:
        return "Add at least one channel."
    seen: list[str] = []
    for entry in channels:
        value = entry.strip()
        if not SLACK_CHANNEL_RE.match(value):
            return "that doesn't look like a channel ID (it starts with C)"
        if value not in seen:
            seen.append(value)
    if len(seen) > MAX_CHANNELS:
        return _CAP_MESSAGE
    return seen


def derive_state(channels: list[str], *, wired: bool) -> str:
    if channels and wired:
        return "connected"
    if channels or wired:
        return "waiting"
    return "idle"
