"""Name-suggestion pool + finalization (create flow v2 addendum): the
generator (`kobo_api.clients.VertexNameClient`) supplies best-effort
candidates; this module is what guarantees the room's contract of "always
exactly five" — filtering each candidate against the agent-name slug rules
and the (taken, reserved) sets, then padding any missing slots from a
curated, deterministically-ordered fallback pool.

Kept separate from `kobo_api.main` (mirrors `kobo_api.avatars`'s split from
the avatar route: pure prompt/logic here, route wiring/response-shape there)
so the matrix below is testable with no FastAPI/DB/HTTP involved at all.
"""

from __future__ import annotations

import hashlib
import itertools

from kobo_api.catalog import validate_name

SUGGESTION_COUNT = 5

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


def _deterministic_order(pool: list[str], seed: str) -> list[str]:
    """A permutation of `pool` that depends only on `seed` — no `random`
    module, no wall-clock, so the exact same `seed` always sorts to the
    exact same order (and a different seed reliably sorts to a different
    one): each name's sort key is `sha256("{seed}:{name}")`, so the whole
    permutation is a pure function of (seed, pool) with nothing external to
    seed a PRNG with.
    """
    return sorted(
        pool, key=lambda name: hashlib.sha256(f"{seed}:{name}".encode()).hexdigest()
    )


def finalize_suggestions(
    candidates: list[dict], *, taken: set[str], reserved: set[str], persona_seed: str
) -> list[dict]:
    """Turn the generator's best-effort `candidates` into up to
    `SUGGESTION_COUNT` `{"name", "reason"}` dicts — exactly five whenever
    enough valid, available names exist between `candidates` and the
    fallback pool (in practice, always: the pool alone is large enough that
    only a pathological amount of prior `taken`/`reserved` overlap could
    leave fewer than five, the "effectively never" case the caller
    (`kobo_api.main`'s name-suggestions route) still guards against with a
    502).

    Each candidate is validated with the exact same slug rule create/
    availability already enforce (`kobo_api.catalog.validate_name` — agent-
    name shape, 2-24 chars) and filtered against `taken`, `reserved`, and
    names already accepted earlier in this same call (de-dupe). Whatever's
    left short of `SUGGESTION_COUNT` is padded from the curated fallback
    pool, walked in a deterministic order derived from `persona_seed` (see
    `_deterministic_order`) — same persona text, same five names, every
    time; a different persona text reliably reorders (and usually
    reselects) the pool.
    """
    result: list[dict] = []
    seen: set[str] = set()

    for candidate in candidates:
        name = candidate.get("name")
        if not isinstance(name, str):
            continue
        name = name.strip()
        if validate_name(name) is not None:
            continue
        if name in taken or name in reserved or name in seen:
            continue
        reason = candidate.get("reason")
        seen.add(name)
        result.append({"name": name, "reason": reason if isinstance(reason, str) else ""})
        if len(result) == SUGGESTION_COUNT:
            return result

    reasons = itertools.cycle(_FALLBACK_REASONS)
    for name in _deterministic_order(_FALLBACK_NAMES, persona_seed):
        if name in taken or name in reserved or name in seen:
            continue
        seen.add(name)
        result.append({"name": name, "reason": next(reasons)})
        if len(result) == SUGGESTION_COUNT:
            return result

    return result
