"""Moving an existing agent to a different colleague mode (issue #131).

The mode lives in two stores: the agents row (`Agent.colleague_mode`, what
the composer and the roster refresh lane read) and the agent's repository
(the allow list in `bundle/agent.yaml` and `bundle/knowledge/colleagues.md`,
what the running agent reads). This module moves the repository half in one
commit; the route moves the row only once that commit has landed.

The commit is never `[skip ci]`: the allow list and the knowledge file reach
the running agent through the rebuild and rollout the generated CI does on
every push. At boot the runtime's seeder drops the knowledge entries of a
file that is gone, so `none` retires the old colleague document too.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from kobo_api.clients import GitHubClient
from kobo_api.composer import update_agent_yaml_colleague_mode

log = logging.getLogger(__name__)


def commit_colleague_mode(
    github: GitHubClient, repo: str, *, mode: str, colleagues_md: str | None
) -> str:
    """Clone `repo`, make its bundle say `mode`, commit and push. Returns
    the pushed head (`github.local_head`); a repo that already says `mode`
    pushes nothing and reports its unchanged head.

    `colleagues_md` is the document to write, rendered by the caller with
    the agent's current roster, or None to remove it (`none` mode).
    Raises `ClientError` on any GitHub or agent.yaml failure.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        bundle = workdir / "bundle"
        changed = update_agent_yaml_colleague_mode(bundle, mode=mode)
        log.debug("colleague mode %s for %s: allow list changed=%s", mode, repo, changed)
        doc = bundle / "knowledge" / "colleagues.md"
        if colleagues_md is None:
            doc.unlink(missing_ok=True)
        else:
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text(colleagues_md)
        github.commit_push(workdir, f"kobo: colleague mode is now {mode}")
        return github.local_head(workdir)
