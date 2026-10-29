"""Data integrity, concurrency: two writes to one agent's repo never run
their clone/commit/push at the same time (issue #268: overlapping pushes
failed as non-fast-forward and rotated the pod once per push)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from kobo_api.skills import save_local_skill
from tests.fakes import FakeGitHub

REPO = "acme/agent-x"
CONTENT = "---\nname: n\ndescription: d\n---\nBody.\n"


class _OverlapCountingGitHub(FakeGitHub):
    """Counts how many writers are between clone and push at once."""

    def __init__(self):
        super().__init__()
        self._guard = threading.Lock()
        self.inside = 0
        self.max_inside = 0

    def clone(self, repo: str, dest: Path) -> None:
        with self._guard:
            self.inside += 1
            self.max_inside = max(self.max_inside, self.inside)
        super().clone(repo, dest)

    def commit_push(self, workdir: Path, message: str) -> bool:
        time.sleep(0.05)  # long enough for an unlocked sibling to overlap
        with self._guard:
            self.inside -= 1
        return super().commit_push(workdir, message)


def test_writes_to_one_repo_queue_instead_of_overlapping():
    github = _OverlapCountingGitHub()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(save_local_skill, github, REPO, f"skill-{i}", CONTENT) for i in range(4)
        ]
        for future in futures:
            future.result()

    assert len(github.pushes) == 4
    assert github.max_inside == 1
