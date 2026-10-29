"""One writer at a time per agent repository.

Every post-assembly lane that edits an agent's repo (skills, tooling,
intake) clones a fresh tempdir, writes, commits and pushes. Route handlers
are plain `def`s, so FastAPI runs them on a thread pool, and two writes to
the same agent could run their clone/commit/push at once: the second push
lands as a non-fast-forward and surfaces as "couldn't reach your agent's
files", and every push that does land starts a CI run that recreates the
pod (issue #268). Holding this lock across the whole clone-to-push window
makes concurrent writes queue instead of race.

Per process, keyed by the repository's full name (one agent, one repo).
Replicas do not share it: two replicas can still race, and the second
push then fails the way it always did. A table-backed lock was judged not
worth it for a window measured in seconds; the UI's batch picker (issue
#266) is what takes most of the overlap away.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_locks: dict[str, threading.Lock] = {}
_registry = threading.Lock()


def _lock_for(repo: str) -> threading.Lock:
    with _registry:
        lock = _locks.get(repo)
        if lock is None:
            lock = _locks[repo] = threading.Lock()
        return lock


@contextmanager
def repo_write_lock(repo: str) -> Iterator[None]:
    """Hold `repo`'s write lock for the block. Not re-entrant: a writer
    must not call another locked writer for the same repo from inside."""
    with _lock_for(repo):
        yield
