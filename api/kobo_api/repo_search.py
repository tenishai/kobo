"""Finding a repository the room's GitHub App can already reach, by typing
part of its name (connected-agents design 2026-09-21).

The connect form used to ask a person to know `owner/name` by heart and
spend a scan finding out whether they had it right. This answers the
smaller question instead: of the repositories this installation can see,
which ones are you starting to type?

Two rules shape everything here.

It offers a short list, never the organisation. An empty query answers with
the eight repositories pushed to most recently, plus the count of how many
there are in all, which is what tells a person the room can already reach
their work; a full dropdown of thousands is not a choice anybody makes, and
it would hand a listing of the organisation to whoever clicked. Eight rows
and a number are a way in, not an index: past them the only way to see more
is to type, and typing is matched by prefix (`_rank`), so no query walks the
organisation either.

It reads GitHub once, not once per keystroke. The listing is the same for
everyone in the room, so one is held for five minutes, and a crowd
arriving at an expired cache costs one read between them rather than one
each -- and only the one caller doing that read waits for it; the rest are
answered from the listing already in hand. Nothing in here is per-caller,
which is why it can be shared. `RepoWarmer` fills it before anyone asks and
keeps filling it every TTL, so nobody pays for the cold read at all: it is
many sequential pages, and the person who opened the form first used to
wait out every one of them.

The ordering is the only judgement this module makes. A repository whose
NAME starts with what was typed is what a person meant; one that merely
contains it somewhere is a fallback. Within a tier, the repository pushed
to most recently wins, because the one worked on last week is looked for
far more often than the one abandoned years ago. An OWNER is matched only
once the query carries a slash -- see `_rank`, where that rule is what
stops a single letter from walking the organisation.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import NamedTuple

from kobo_api.clients import GitHubClient, InstallationRepo

log = logging.getLogger(__name__)

# How long one listing is held. Long enough that a form filled in over a
# couple of minutes costs one GitHub read, short enough that a repository
# created (or granted to the App) during a working session shows up without
# anyone restarting anything.
TTL_S = 5 * 60

# The longest query the route will take. No repository name is anywhere
# near this, so past it a caller is not searching, and a bound is cheaper
# than reasoning about what a megabyte of query would do here.
MAX_QUERY_CHARS = 100

# The most rows the field will ever show. A person scanning a list this
# long is typing one more character, not reading further, and the count of
# what was left out is reported instead.
MAX_MATCHES = 10

# The most rows an empty query gets, which is fewer than a search gets on
# purpose. These are not matches for anything -- nobody asked for them --
# so they are a glance that says "the room reaches your work, start here",
# and a glance is short. The count beside them carries the rest.
RECENT_MATCHES = 8

# How long the warmer waits before joining its thread on the way out. The
# thread is a daemon and holds nothing anyone else needs, so this is only
# the courtesy of letting a listing already in flight finish; past it,
# shutdown carries on without it.
WARM_STOP_TIMEOUT_S = 5.0

# Match tiers, best first. Only their ORDER matters.
_RANK_NAME_PREFIX = 0
_RANK_FULL_NAME_PREFIX = 1
_RANK_NAME_SUBSTRING = 2


class RepoMatches(NamedTuple):
    """One answer from `RepoIndex.search`.

    `total` is on every answer, not just the empty one: the field says how
    many repositories the room reaches, and it must be able to say it
    without a second question that could straddle a refresh and disagree
    with the rows it is sitting under.
    """

    repos: list[InstallationRepo]
    truncated: bool
    total: int


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
        # Two locks, doing two different jobs. `_state_lock` is held for
        # the length of a few assignments and never across a GitHub call.
        # `_refresh_lock` is the one refresh at a time, and is TRIED
        # rather than waited on by anyone who already has a listing to
        # answer with.
        self._state_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._repos: list[InstallationRepo] = []
        self._loaded_at: float | None = None

    def warm(self, now: float | None = None) -> None:
        """Fill the listing if it is missing or past its TTL, off anybody's
        request. Raises the same way `search` does; `RepoWarmer` is what
        decides that a failure here is a logged line rather than an error
        somebody sees."""
        self._listing(self._now() if now is None else now)

    def search(self, query: str, now: float | None = None) -> RepoMatches:
        """The repositories matching `query`, best first, whether more
        matched than came back, and how many the room reaches in all.

        An empty query is not a search but it is still an answer: the
        `RECENT_MATCHES` pushed to most recently, so a person who has not
        typed yet can see that the room already reaches their work. It
        costs no GitHub read against a listing already in hand -- only a
        cold cache, which is the same read the first keystroke would have
        paid for, and which the warmer normally gets to first.

        Raises whatever the client raises when the listing cannot be read.
        An installation with no repositories and a token that may not ask
        are different facts, and the route says so differently.
        """
        repos = self._listing(self._now() if now is None else now)
        total = len(repos)

        needle = query.strip().lower()
        if not needle:
            recent = sorted(repos, key=lambda repo: repo.pushed_at, reverse=True)
            return RepoMatches(recent[:RECENT_MATCHES], total > RECENT_MATCHES, total)

        matched: list[tuple[int, InstallationRepo]] = []
        for repo in repos:
            rank = _rank(repo, needle)
            if rank is not None:
                matched.append((rank, repo))

        # Two sorts rather than one composite key: `pushed_at` is a string
        # and wants reversing, the rank is a number and does not, and
        # Python's sort is stable, so the recency order set here survives
        # the ranking pass on top of it.
        matched.sort(key=lambda pair: pair[1].pushed_at, reverse=True)
        matched.sort(key=lambda pair: pair[0])

        ranked = [repo for _, repo in matched]
        return RepoMatches(
            ranked[:MAX_MATCHES], len(ranked) > MAX_MATCHES, total
        )

    def _listing(self, now: float) -> list[InstallationRepo]:
        """The cached listing, refreshed when it is missing or past the
        TTL -- without making anybody wait who does not have to.

        A stale listing is still an answer. This route is synchronous, so
        a caller waiting on GitHub is a worker thread pinned in a pool of
        forty; if an expiry made every request in flight wait for one
        listing, a slow GitHub would stall the whole API rather than just
        this field. So exactly one caller refreshes (the one that wins
        `_refresh_lock`, tried and not waited on) and everyone else is
        answered from the listing already in hand, a few minutes old,
        which is what a typeahead over repository names can afford to be.

        Only a COLD cache blocks, because there is nothing else to say.

        A refresh that raises leaves the load stamp alone, so the next
        caller tries again rather than inheriting a failure for five
        minutes.
        """
        repos, loaded_at = self._snapshot()
        if loaded_at is not None and now - loaded_at < self._ttl_s:
            return repos

        if loaded_at is None:
            with self._refresh_lock:
                # Checked again inside: the caller that was waiting here
                # while the winner refreshed now has an answer and must
                # not read GitHub a second time for it.
                repos, loaded_at = self._snapshot()
                if loaded_at is not None:
                    return repos
                return self._refresh(now)

        if not self._refresh_lock.acquire(blocking=False):
            return repos
        try:
            repos, loaded_at = self._snapshot()
            if loaded_at is not None and now - loaded_at < self._ttl_s:
                return repos
            return self._refresh(now)
        finally:
            self._refresh_lock.release()

    def _snapshot(self) -> tuple[list[InstallationRepo], float | None]:
        with self._state_lock:
            return self._repos, self._loaded_at

    def _refresh(self, now: float) -> list[InstallationRepo]:
        """Reads GitHub and installs the result. Called with
        `_refresh_lock` held, and never with `_state_lock` held: the read
        is the slow part, and nothing else may wait on it."""
        # Archived repositories are dropped here rather than per search:
        # they cannot be connected, so they are not part of the answer to
        # any query.
        repos = [
            repo for repo in self._github.list_installation_repos() if not repo.archived
        ]
        with self._state_lock:
            self._repos = repos
            self._loaded_at = now
        return repos


class RepoWarmer:
    """Keeps one `RepoIndex` filled for as long as the process lives.

    The cache used to fill on the first search, which meant the first
    person to open the connect form waited out every page of the
    installation's listing before a single row appeared. Nothing about
    that read is per-caller, so nobody needs to be there for it: this
    fills the listing at boot and re-fills it every TTL, and by the time
    anyone types, the answer is already in hand.

    A daemon thread, deliberately. It holds nothing, writes nothing
    outside the index, and must never be the reason a process will not
    exit -- the lifespan calls `stop` on the way out, and a listing still
    in flight past `WARM_STOP_TIMEOUT_S` is simply abandoned.

    `wait` is the seam a test drives this through: the default is the stop
    event's own wait, which is both the sleep between rounds and the way
    shutdown interrupts it instantly rather than after five minutes.
    """

    def __init__(
        self,
        index: RepoIndex,
        ttl_s: float = TTL_S,
        wait: Callable[[float], bool] | None = None,
    ):
        self._index = index
        self._ttl_s = ttl_s
        self._stop = threading.Event()
        # Bound here rather than defaulted in the signature: the event it
        # waits on does not exist until this line has run.
        self._wait = wait if wait is not None else self._stop.wait
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        thread = threading.Thread(target=self.run, name="repo-warm", daemon=True)
        self._thread = thread
        thread.start()

    def run(self) -> None:
        """Warm, wait a TTL, warm again, until asked to stop. Warms FIRST:
        boot is exactly when the listing is coldest."""
        while True:
            self.warm_once()
            if self._wait(self._ttl_s):
                return

    def warm_once(self) -> None:
        """One refresh, with no way to fail loudly. Nobody is waiting on
        this thread, so a GitHub that will not answer is a logged line and
        another try one TTL later -- and in the meantime the next search
        fills the listing itself, exactly as it did before there was a
        warmer at all.

        Broad by design, the same way `create_agent`'s launch guard in
        main.py is: this is a thread boundary rather than one expected
        failure mode, and anything that escapes it kills the warmer for
        the life of the process.
        """
        try:
            self._index.warm()
        except Exception as exc:
            log.warning(
                "could not warm the repository listing, the next search will try: %s",
                exc,
            )

    def stop(self, timeout_s: float = WARM_STOP_TIMEOUT_S) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_s)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def _rank(repo: InstallationRepo, needle: str) -> int | None:
    """Which tier `repo` matches `needle` in, or None for no match.

    The owner is only ever matched by a query that carries a slash, and
    that is the rule holding this route to what it promises. Nearly
    everything one installation can see shares an owner, so a query
    matched against `owner/name` from its first letter would answer `e`
    with the whole organisation, ten rows and a "more matched" at a time
    -- the list-all this field does not offer, reachable one keystroke at
    a time. Before the slash the query is a name and only the name tiers
    apply; after it, the query names an owner and only the full name
    does.

    The substring tier reads the name for the same reason, and only the
    name: an owner shared by everything is not a distinguishing thing to
    have typed.
    """
    full_name = repo.full_name.lower()
    _, _, name = full_name.partition("/")
    name = name or full_name
    if "/" in needle:
        return _RANK_FULL_NAME_PREFIX if full_name.startswith(needle) else None
    if name.startswith(needle):
        return _RANK_NAME_PREFIX
    if needle in name:
        return _RANK_NAME_SUBSTRING
    return None
