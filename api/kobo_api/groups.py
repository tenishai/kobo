"""Group membership resolution (access-control arc, Task B1): answers "which
CONFIGURED groups does this email belong to" by asking the directory service
(Okta, today), cached per email, fail-closed.

Fail-closed contract (`GroupResolver.groups_for`): `None` means the resolver
itself failed to get an answer -- an unreachable directory, an auth/server
error, a timeout -- and is DISTINCT from `[]`, which means the directory
answered and this email genuinely belongs to none of the configured groups.
Every caller MUST treat `None` as "grant nothing" (Global Constraints: Okta
unreachable means group-derived visibility vanishes, owner + operator rights
remain; never wider) -- `[]` and `None` must never be collapsed into the same
branch, or a directory outage would silently read as "belongs to nothing"
instead of "couldn't tell", and a caller that grants on `not None` would get
that backwards.

IDP-agnostic seam: `GroupResolver` is a `typing.Protocol`, not a base class,
and names no vendor. `OktaGroupResolver` is the one implementation today; a
future IdP (Google Workspace, Entra ID, ...) is one new class implementing
the same `groups_for` shape -- no change needed to any caller that only ever
depends on the Protocol.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)


@runtime_checkable
class GroupResolver(Protocol):
    def groups_for(self, email: str) -> list[str] | None:
        """CONFIGURED group names `email` currently belongs to.

        `None` means the resolver failed to answer at all (see module
        docstring: fail-closed, callers grant nothing) -- distinct from an
        empty list, a successful "belongs to none of them" answer.
        """
        ...


def _invert_universe(universe: dict[str, list[str]]) -> dict[str, str]:
    """Build the okta-name -> config-name mapping `OktaGroupResolver` tests
    membership against, from the constructor's config-name -> [okta-names]
    input (`Config.groups`/`kobo_api.clients.Clients.real` -- the directory
    splits one human team across several regional groups, so the config
    side is naturally a list; membership only ever needs the reverse
    direction: one Okta group name to the single CONFIG name it backs).

    Raises `ValueError` if the same Okta group name is claimed by two
    DIFFERENT config names -- an ambiguous mapping the old
    dict-keyed-by-config-name shape could silently produce (last write
    wins) but could never even represent once inverted, so it must be
    rejected here instead. The same Okta name appearing twice under the
    SAME config name (a literal duplicate in one entry's list, or the same
    regional group reachable via both `okta_group` and `okta_groups` sugar)
    is not an error -- the second assignment just overwrites the first with
    an identical value.
    """
    inverted: dict[str, str] = {}
    for config_name, okta_names in universe.items():
        for okta_name in okta_names:
            existing = inverted.get(okta_name)
            if existing is not None and existing != config_name:
                raise ValueError(
                    f"okta group {okta_name!r} is claimed by both "
                    f"{existing!r} and {config_name!r} -- a directory group "
                    "can only ever back one room group"
                )
            inverted[okta_name] = config_name
    return inverted


class OktaGroupResolver:
    """`GroupResolver` backed by Okta's Users API.

    `GET https://{domain}/api/v1/users/{email}/groups` (email URL-escaped
    into the path -- see `_fetch` below), header `Authorization: SSWS
    {token}`. The response is a JSON list of group objects whose display
    name lives at `profile.name`.

    `universe` (the constructor argument) is CONFIG group name -> LIST of
    Okta group names, matching `Config.groups`/`GroupChoice.okta_groups`
    shape directly (`kobo_api.clients.Clients.real` builds it straight from
    `config.groups`, one list entry per `GroupChoice`, already merged for
    any repeated `name`). Construction inverts this into `self._universe`,
    Okta name -> CONFIG name (`_invert_universe` above) -- that's the
    direction `_fetch` actually needs: for every Okta group the member is
    in, look up the one CONFIG name (if any) it backs. Both the filter
    (only membership in one of these Okta groups counts) and the
    translation (the CONFIG name, not Okta's, is what's returned -- callers
    never see an Okta group name directly) fall out of that single lookup.
    A member of several regional Okta groups that all back the same CONFIG
    name is deduped to that one CONFIG name (see `_fetch`).

    Every failure mode -- non-200 (401/403/500/...), a network error, a
    timeout, a malformed body -- is caught here and answered as `None`;
    nothing this class does ever raises to a caller. Each failure is logged
    (status code and/or a truncated response body, or the exception's
    `repr`) but the token itself is never part of what's logged: it lives
    only in the request's `Authorization` header, which neither an httpx
    exception's message nor a response body ever echoes back.

    Successful answers are cached per email for `ttl_s` seconds (a plain
    dict, `email -> (expires_at, result)`). A failed lookup is cached too,
    but only for the much shorter `failure_ttl_s` floor, in a separate dict
    keyed by email -- a flapping/slow directory would otherwise make every
    group-needing request pay the full HTTP timeout for the whole outage.
    That floor never evicts or shadows a fresh success: `groups_for` only
    ever reaches the failure check (and the network) once a cached success
    is either absent or already past its own TTL, so the two stores can
    never race to overwrite each other's answer. A success once past its
    own TTL is never re-served just because the refetch that would have
    replaced it failed -- fail-closed wins there too. `client` and `now` are
    test seams only (a real `httpx.Client` and `time.time` by default), same
    convention as `kobo_api.github_app.InstallationTokenProvider`.

    `email` is lowercased once at the `groups_for` boundary -- both the
    cache key and the URL path this sends to Okta -- matching the rest of
    the house's convention (`main.py`'s operator checks) that an IAP
    email's case is preserved but an Okta identity is case-insensitive.
    """

    def __init__(
        self,
        domain: str,
        token: str,
        universe: dict[str, list[str]],
        ttl_s: int = 900,
        failure_ttl_s: int = 60,
        client: httpx.Client | None = None,
        now: Callable[[], float] | None = None,
    ):
        self._domain = domain
        self._token = token
        # Inverted at construction (okta name -> config name) -- see
        # `_invert_universe`'s docstring for why, and for the ValueError
        # this raises on a cross-config-name collision.
        self._universe = _invert_universe(universe)
        self._ttl_s = ttl_s
        self._failure_ttl_s = failure_ttl_s
        # A single small JSON GET, not a model roundtrip -- same "quick
        # JSON-API CRUD call" family as HttpxStorageClient (kobo_api.clients),
        # whose 20s/10s-connect default this mirrors: real headroom over
        # httpx's bare 5s, nowhere near the generous timeouts a model call
        # needs.
        self._client = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0))
        self._now = now or time.time
        self._cache: dict[str, tuple[float, list[str]]] = {}
        # Separate from `_cache` on purpose: a failure marker must live
        # beside, never instead of, whatever (possibly stale) success entry
        # is already there -- see the class docstring.
        self._failed_until: dict[str, float] = {}

    def groups_for(self, email: str) -> list[str] | None:
        email = email.lower()
        now = self._now()
        cached = self._cache.get(email)
        if cached is not None:
            expires_at, result = cached
            if expires_at > now:
                return result

        failed_until = self._failed_until.get(email)
        if failed_until is not None and failed_until > now:
            return None

        result = self._fetch(email)
        if result is not None:
            self._cache[email] = (now + self._ttl_s, result)
            self._failed_until.pop(email, None)
        else:
            self._failed_until[email] = now + self._failure_ttl_s
        return result

    def _fetch(self, email: str) -> list[str] | None:
        # Deliberate, explicit percent-encoding of the path segment (rather
        # than relying on however httpx's own URL parser would treat an
        # interpolated raw string) -- an email with a "+" tag
        # (someone+tag@example.com) or other reserved-looking character
        # must land in the path as the literal email Okta expects, not
        # something httpx or an intermediate proxy reinterprets.
        url = f"https://{self._domain}/api/v1/users/{quote(email, safe='')}/groups"
        try:
            resp = self._client.get(url, headers={"Authorization": f"SSWS {self._token}"})
        except httpx.HTTPError as exc:
            # repr(exc) here is safe: an httpx transport exception's message
            # never echoes the request's headers, only method/URL at most --
            # the token (sent only via the Authorization header) never rides
            # along.
            log.warning("group lookup request failed: %r", exc)
            return None

        if resp.status_code != 200:
            log.warning(
                "group lookup failed (%s): %s", resp.status_code, resp.text[:200]
            )
            return None

        try:
            groups = resp.json()
        except ValueError as exc:
            log.warning("malformed group lookup reply: %r (body: %s)", exc, resp.text[:200])
            return None

        if not isinstance(groups, list):
            log.warning("group lookup reply wasn't a list (got %s)", type(groups).__name__)
            return None

        okta_names = set()
        for group in groups:
            if not isinstance(group, dict):
                continue
            profile = group.get("profile")
            name = profile.get("name") if isinstance(profile, dict) else None
            if isinstance(name, str):
                okta_names.add(name)

        # `self._universe` is Okta name -> CONFIG name (inverted at
        # construction, see `_invert_universe`). A member of several
        # regional Okta groups that all back the same CONFIG name (the
        # directory-splits-one-team-by-region case `GroupChoice.okta_groups`
        # exists for) must yield that CONFIG name exactly once -- the `not
        # in config_names` guard is the dedup, order-of-first-sight rather
        # than sorted, since no caller depends on a particular order.
        config_names: list[str] = []
        for okta_name in okta_names:
            config_name = self._universe.get(okta_name)
            if config_name is not None and config_name not in config_names:
                config_names.append(config_name)
        return config_names
