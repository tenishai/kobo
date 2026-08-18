"""purpose_tags plumbing: the same "pure helpers, routes own persistence"
split as kobo_api.slack_state, just for an agent's purpose tags rather than
its Slack channel list — JSON encode/decode for the Agent.purpose_tags
column, plus the shared normalization core every caller of purpose_tags
runs through, whether that's a model-drafted list
(kobo_api.persona_draft.finalize_draft) or a user-submitted one (the
POST /api/agents route in kobo_api.main).
"""

from __future__ import annotations

import json

# The create route's own cap (persona-metadata plan, Task 2): a
# user-submitted purpose_tags list that's still over this count AFTER
# normalization (de-dupe can bring an over-cap raw list back under) is
# rejected outright rather than silently truncated — unlike the draft's own
# cap (kobo_api.persona_draft, capped at 5), which is a best-effort model
# suggestion, not something a caller typed and would be surprised to see
# silently cut. Mirrors kobo_api.slack_state's own MAX_CHANNELS/
# _CAP_MESSAGE idiom exactly.
MAX_CREATE_TAGS = 8
_CAP_MESSAGE = "Eight tags at most."


def normalize_tags(tags: object) -> list[str]:
    """Lowercase, strip, drop empties, de-duplicate (first occurrence wins,
    order preserved). Any non-list input, or any non-string entry within
    it, is dropped rather than raised on — this runs against both a
    user-submitted JSON list (pydantic already guarantees `list[str]` at
    the create route, but defense-in-depth costs nothing) and a model's
    best-effort reply (kobo_api.clients.VertexNameClient.draft, which does
    no type/bounds checking of its own).

    No cap applied here — callers apply their own: the draft silently
    truncates to 5 (a best-effort suggestion), the create route caps at 8
    with an outright rejection instead of truncating (see
    `MAX_CREATE_TAGS`/`_CAP_MESSAGE` above and `clean_tags` below) — a
    caller who typed 9 tags would be surprised to have the 9th silently
    vanish.
    """
    if not isinstance(tags, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            continue
        cleaned = tag.strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def clean_tags(tags: object) -> list[str] | str:
    """The normalized list, or a plain-copy error string when it's still
    over `MAX_CREATE_TAGS` after normalization — the cap is checked AFTER
    de-dupe, so repeats of the same tag never trip it, same as
    kobo_api.slack_state.clean_channels's own cap check. Same union-return
    idiom as that function too: a caller checks `isinstance(result, str)`
    to tell a rejection from a real (possibly empty) list.
    """
    normalized = normalize_tags(tags)
    if len(normalized) > MAX_CREATE_TAGS:
        return _CAP_MESSAGE
    return normalized


def decode_tags(raw: str | None) -> list[str]:
    """The stored purpose_tags column, decoded back to a list — `[]` for a
    null column (a pre-existing row, or one created before any tags were
    given), same as kobo_api.slack_state.decode_channels."""
    if not raw:
        return []
    return [str(tag) for tag in json.loads(raw)]


def encode_tags(tags: list[str]) -> str:
    """JSON-encode `tags` for storage on the Agent.purpose_tags column."""
    return json.dumps(tags)
