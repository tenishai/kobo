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


class OktaGroupResolver:
    """`GroupResolver` backed by Okta's Users API.

    `GET https://{domain}/api/v1/users/{email}/groups` (email URL-escaped
    into the path -- see `_fetch` below), header `Authorization: SSWS
    {token}`. The response is a JSON list of group objects whose display
    name lives at `profile.name`; `universe` (config group name -> Okta
    group name, `Config.groups`/`kobo_api.clients.Clients.real`) is both the
    filter (only membership in one of these Okta groups counts) and the
    translation (the CONFIG name, not Okta's, is what's returned -- callers
    never see an Okta group name directly).

    Every failure mode -- non-200 (401/403/500/...), a network error, a
    timeout, a malformed body -- is caught here and answered as `None`;
    nothing this class does ever raises to a caller. Each failure is logged
    (status code and/or a truncated response body, or the exception's
    `repr`) but the token itself is never part of what's logged: it lives
    only in the request's `Authorization` header, which neither an httpx
    exception's message nor a response body ever echoes back.

    Successful answers are cached per email for `ttl_s` seconds (a plain
    dict, `email -> (expires_at, result)`); a failed lookup is never cached,
    so the very next call retries the directory rather than replaying a
    stale outage for the rest of the TTL window. `client` and `now` are test
    seams only (a real `httpx.Client` and `time.time` by default), same
    convention as `kobo_api.github_app.InstallationTokenProvider`.
    """

    def __init__(
        self,
        domain: str,
        token: str,
        universe: dict[str, str],
        ttl_s: int = 900,
        client: httpx.Client | None = None,
        now: Callable[[], float] | None = None,
    ):
        self._domain = domain
        self._token = token
        self._universe = universe
        self._ttl_s = ttl_s
        # A single small JSON GET, not a model roundtrip -- same "quick
        # JSON-API CRUD call" family as HttpxStorageClient (kobo_api.clients),
        # whose 20s/10s-connect default this mirrors: real headroom over
        # httpx's bare 5s, nowhere near the generous timeouts a model call
        # needs.
        self._client = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0))
        self._now = now or time.time
        self._cache: dict[str, tuple[float, list[str]]] = {}

    def groups_for(self, email: str) -> list[str] | None:
        now = self._now()
        cached = self._cache.get(email)
        if cached is not None:
            expires_at, result = cached
            if expires_at > now:
                return result

        result = self._fetch(email)
        if result is not None:
            self._cache[email] = (now + self._ttl_s, result)
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

        return [
            config_name
            for config_name, okta_name in self._universe.items()
            if okta_name in okta_names
        ]
