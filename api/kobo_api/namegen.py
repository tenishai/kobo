"""Name-suggestion pool + finalization (create flow v2 addendum): the
generator (`kobo_api.clients.VertexNameClient`) supplies best-effort
candidates; this module is what guarantees the room's contract of "always
exactly `SUGGESTION_COUNT`" -- filtering each candidate against the agent-name
slug rules and the (taken, reserved, exclude) sets, then padding any missing
slots from a curated, deterministically-ordered fallback pool.

Kept separate from `kobo_api.main` (mirrors `kobo_api.avatars`'s split from
the avatar route: pure prompt/logic here, route wiring/response-shape there)
so the matrix below is testable with no FastAPI/DB/HTTP involved at all.
"""

from __future__ import annotations

import hashlib
import itertools

from kobo_api.catalog import validate_name

SUGGESTION_COUNT = 3

# Curated fallback pool (~24, workshop/nature/myth-flavored, all lowercase
# single words, no client/company names anywhere) — the room's answer when
# the generator errors out, or when its candidates don't leave enough usable
# slots after filtering. Definition order here is irrelevant: presentation
# order always comes from `_deterministic_order` below, keyed off the
# persona text, never this list's own order.
_FALLBACK_NAMES = [
    "ember", "cedar", "flint", "loom", "anvil", "birch", "wren", "hollow",
    "juniper", "quill", "rowan", "slate", "moss", "kiln", "amber", "thistle",
    "briar", "hearth", "linden", "vesper", "sable", "clove", "reed", "forge",
]

# Fixed reasons cycled onto fallback picks in the order they're accepted —
# never tied to any one name (a name earns whichever reason is next in the
# cycle, purely a function of how many fallback slots have already been
# filled this call), so the pairing stays deterministic for a given persona
# seed without needing a reason-per-name table to maintain.
_FALLBACK_REASONS = [
    "a steady hand",
    "quiet and quick",
    "sharp and precise",
    "warm at the bench",
    "never in a hurry",
    "built to last",
    "keeps things simple",
    "shows up early",
]


def _deterministic_order(names: list[str], persona_seed: str, round_salt: str) -> list[str]:
    """A permutation of `names` that depends only on `(persona_seed,
    round_salt)` -- no `random` module, no wall-clock, so the exact same pair
    always sorts to the exact same order (and a different `persona_seed` or
    a different `round_salt` reliably sorts to a different one): each name's
    sort key is `sha256("{persona_seed}:{round_salt}:{name}")`, so the whole
    permutation is a pure function of (persona_seed, round_salt, names) with
    nothing external to seed a PRNG with. `round_salt` is what lets a caller
    ask for a fresh-looking fallback order on a retry within the same
    persona (e.g. "give me another batch") without giving up determinism
    within any one round.
    """
    return sorted(
        names,
        key=lambda name: hashlib.sha256(
            f"{persona_seed}:{round_salt}:{name}".encode()
        ).hexdigest(),
    )


def _normalize_reason(reason: str) -> str:
    """Normalize a generator-supplied reason string for safe UI rendering.

    - Strips leading/trailing whitespace
    - Collapses internal whitespace runs to single spaces
    - Truncates to 80 characters with smart word-boundary logic:
      * If within the last 20 chars (positions 60-80) there's a space,
        cut at that space to avoid mid-word truncation
      * Otherwise, hard cut at exactly 80 chars

    Returns the normalized string, which may be empty (if input was whitespace-only).
    """
    # Strip and collapse all internal whitespace runs to single spaces
    normalized = " ".join(reason.split())

    # If within acceptable length, done
    if len(normalized) <= 80:
        return normalized

    # Need to truncate; look for a word boundary (space) in the last 20 chars
    # Search backwards from position 80 to position 60 (the last 20 chars)
    for i in range(80, 59, -1):  # 80 down to 60 inclusive
        if i < len(normalized) and normalized[i] == " ":
            # Found a space; cut here and trim any trailing space
            return normalized[:i].rstrip()

    # No word boundary found in last 20 chars; hard cut at 80
    return normalized[:80]


def finalize_suggestions(
    candidates: list[dict],
    *,
    taken: set[str],
    reserved: set[str],
    persona_seed: str,
    exclude: set[str] | frozenset[str] = frozenset(),
    round_salt: str = "0",
) -> list[dict]:
    """Turn the generator's best-effort `candidates` into up to
    `SUGGESTION_COUNT` `{"name", "reason"}` dicts -- exactly `SUGGESTION_COUNT`
    whenever enough valid, available names exist between `candidates` and
    the fallback pool (in practice, always: the pool alone is large enough
    that only a pathological amount of prior `taken`/`reserved`/`exclude`
    overlap could leave fewer than that, the "effectively never" case the
    caller (`kobo_api.main`'s name-suggestions route) still guards against
    with a 502).

    Each candidate is validated with the exact same slug rule create/
    availability already enforce (`kobo_api.catalog.validate_name` — agent-
    name shape, 2-24 chars) and filtered against `taken`, `reserved`,
    `exclude`, and names already accepted earlier in this same call
    (de-dupe). `exclude` is the set of names already offered to the caller
    this session (e.g. an earlier round's suggestions the user asked to
    regenerate past) -- normalized to lowercase before comparison, since
    unlike `taken`/`reserved` it isn't guaranteed to arrive pre-normalized,
    and dropped exactly like `taken`/`reserved`. Whatever's left short of
    `SUGGESTION_COUNT` is padded from the curated fallback pool, walked in a
    deterministic order derived from `(persona_seed, round_salt)` (see
    `_deterministic_order`) -- same persona text and round, same names, every
    time; a different persona text or a different `round_salt` reliably
    reorders (and usually reselects) the pool. `round_salt` is what lets
    consecutive fallback-only rounds (same persona, nothing from the
    generator survives `exclude`) rotate instead of repeating the same
    padding every time.

    Generator-supplied reason strings are normalized for safe UI rendering:
    whitespace is collapsed to single spaces, leading/trailing whitespace
    stripped, and length is clamped to 80 characters (with smart word-
    boundary truncation). Reasons that become empty after normalization are
    replaced with entries from the fallback reason cycle.
    """
    excluded = {name.strip().lower() for name in exclude}
    result: list[dict] = []
    seen: set[str] = set()
    reasons = itertools.cycle(_FALLBACK_REASONS)

    for candidate in candidates:
        name = candidate.get("name")
        if not isinstance(name, str):
            continue
        name = name.strip()
        if validate_name(name) is not None:
            continue
        if name in taken or name in reserved or name in excluded or name in seen:
            continue
        reason = candidate.get("reason")
        if isinstance(reason, str):
            reason = _normalize_reason(reason)
            if not reason:  # empty after normalization
                reason = next(reasons)
        else:
            reason = ""
        seen.add(name)
        result.append({"name": name, "reason": reason})
        if len(result) == SUGGESTION_COUNT:
            return result

    for name in _deterministic_order(_FALLBACK_NAMES, persona_seed, round_salt):
        if name in taken or name in reserved or name in excluded or name in seen:
            continue
        seen.add(name)
        result.append({"name": name, "reason": next(reasons)})
        if len(result) == SUGGESTION_COUNT:
            return result

    return result
