"""Finding a repository the room's GitHub App can already reach, by typing
part of its name (connected-agents design 2026-09-21).

The connect form used to ask a person to know `owner/name` by heart and
spend a scan finding out whether they had it right. This answers the
smaller question instead: of the repositories this installation can see,
which ones are you starting to type?

Two rules shape everything here.

It never offers a list. An empty query answers with nothing, and the field
shows nothing on focus or on a click. The installation can see thousands of
repositories and a dropdown of all of them is not a choice anybody makes;
it is also a listing of the organisation handed to whoever clicked, and
this route is open to any signed-in person precisely because it only ever
names what somebody was already typing.

It reads GitHub once, not once per keystroke. The listing is the same for
everyone in the room, so one is held for five minutes behind a lock, and a
crowd arriving at an expired cache costs one read between them rather than
one each. Nothing in here is per-caller, which is why it can be shared.

The ordering is the only judgement this module makes. A repository whose
NAME starts with what was typed is what a person meant; one whose full
`owner/name` starts with it is the next most likely; one that merely
contains it somewhere is a fallback. Within a tier, the repository pushed
to most recently wins, because the one worked on last week is looked for
far more often than the one abandoned years ago.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from kobo_api.clients import GitHubClient, InstallationRepo

# How long one listing is held. Long enough that a form filled in over a
# couple of minutes costs one GitHub read, short enough that a repository
# created (or granted to the App) during a working session shows up without
# anyone restarting anything.
TTL_S = 5 * 60

# The most rows the field will ever show. A person scanning a list this
# long is typing one more character, not reading further, and the count of
# what was left out is reported instead.
MAX_MATCHES = 10

# Match tiers, best first. Only their ORDER matters.
_RANK_NAME_PREFIX = 0
_RANK_FULL_NAME_PREFIX = 1
_RANK_NAME_SUBSTRING = 2


class RepoIndex:
    """The installation's repositories, cached, searchable by prefix.

    One per process, held on the app (`app.state.repo_index`). `now` is
    passed into `search` rather than read here so the TTL is exactly
    pinnable in a test; the constructor's own `now` is only the default
    for callers that have no clock of their own.
    """

    def __init__(
        self,
        github: GitHubClient,
        ttl_s: float = TTL_S,
        now: Callable[[], float] = time.monotonic,
    ):
        self._github = github
        self._ttl_s = ttl_s
        self._now = now
        self._lock = threading.Lock()
        self._repos: list[InstallationRepo] = []
        self._loaded_at: float | None = None

    def search(
        self, query: str, now: float | None = None
    ) -> tuple[list[InstallationRepo], bool]:
        """The repositories matching `query`, best first, and whether more
        matched than came back.

        An empty query is not a search: it answers with nothing and reads
        nothing, so a focused field costs the room a GitHub listing only
        once somebody actually types.

        Raises whatever the client raises when the listing cannot be read.
        An installation with no repositories and a token that may not ask
        are different facts, and the route says so differently.
        """
        needle = query.strip().lower()
        if not needle:
            return [], False

        matched: list[tuple[int, InstallationRepo]] = []
        for repo in self._listing(self._now() if now is None else now):
            rank = _rank(repo, needle)
            if rank is not None:
                matched.append((rank, repo))

        # Two sorts rather than one composite key: `pushed_at` is a string
        # and wants reversing, the rank is a number and does not, and
        # Python's sort is stable, so the recency order set here survives
        # the ranking pass on top of it.
        matched.sort(key=lambda pair: pair[1].pushed_at, reverse=True)
        matched.sort(key=lambda pair: pair[0])

        repos = [repo for _, repo in matched]
        return repos[:MAX_MATCHES], len(repos) > MAX_MATCHES

    def _listing(self, now: float) -> list[InstallationRepo]:
        """The cached listing, refreshed first when it is missing or older
        than the TTL.

        The refresh happens INSIDE the lock, so a crowd arriving together
        makes one GitHub read between them and every one of them gets the
        fresh answer. A refresh that raises leaves the previous load stamp
        alone, so the next caller tries again rather than inheriting a
        failure for five minutes.
        """
        with self._lock:
            if self._loaded_at is None or now - self._loaded_at >= self._ttl_s:
                # Archived repositories are dropped here rather than per
                # search: they cannot be connected, so they are not part of
                # the answer to any query.
                self._repos = [
                    repo
                    for repo in self._github.list_installation_repos()
                    if not repo.archived
                ]
                self._loaded_at = now
            return self._repos


def _rank(repo: InstallationRepo, needle: str) -> int | None:
    """Which tier `repo` matches `needle` in, or None for no match.

    The substring tier reads the name only. `owner/` is the same on
    everything an installation can see, so a substring match over the full
    name would hand back the whole organisation on the first keystroke of
    its own name.
    """
    full_name = repo.full_name.lower()
    _, _, name = full_name.partition("/")
    name = name or full_name
    if name.startswith(needle):
        return _RANK_NAME_PREFIX
    if full_name.startswith(needle):
        return _RANK_FULL_NAME_PREFIX
    if needle in name:
        return _RANK_NAME_SUBSTRING
    return None
