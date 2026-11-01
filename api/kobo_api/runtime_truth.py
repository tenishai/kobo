"""What an agent is built on, as a fact the room records (2026-11-01).

`Agent.runtime_image` is a CHOICE: NULL means "follow the room's pin", a
value means "hold this one". For years of agents that choice has been NULL,
and the Harness page resolved it as `agent.runtime_image or
config.runtime_image`, then compared that with `config.runtime_image`: the
room's pin against itself. So when the room moved to a new runtime, every
agent that followed the room read "Runtime is up to date" while it still
ran an image built on the old one, and the page offered nothing to press.

`Agent.built_runtime_image` is the FACT: the ref the agent repository's
Dockerfile `FROM` line carries right now. Every path that writes that line
records it here in the same change (compose at create, each fast-lane
re-pin, the runtime route), and this module fills it at boot for the rows
that predate the column by reading the file itself. The page compares THIS
with the room's pin.

Boot backfill rules: off the request path (a daemon thread), one read per
agent that has no fact yet, never raises, one summary line in the log, and
an agent whose file cannot be read today is simply tried again next boot.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session, sessionmaker

from kobo_api.clients import GitHubClient
from kobo_api.connect_scan import DOCKERFILE, _base_image
from kobo_api.kinds import KIND_DIRECTORY
from kobo_api.models import Agent

log = logging.getLogger(__name__)


def dockerfile_runtime(text: str) -> str | None:
    """The ref the FINAL stage of `text` builds on, or None when there is
    no FROM line. The room's own Dockerfile has exactly one FROM; a
    connected agent's may have several, and the connect scan's rule (the
    last stage, following stage names) is the right one for both."""
    return _base_image(text)


def record_built_runtime(session_factory: sessionmaker[Session], repo: str, runtime_image: str) -> None:
    """Record that `repo`'s Dockerfile now builds on `runtime_image`, for
    a caller that has no agent row in hand (the background archive lanes).
    Its own short transaction; never raises, because the commit it reports
    on has already landed and a bookkeeping miss must not fail the lane.
    The next boot's backfill, or the next commit, puts it right."""
    try:
        with session_factory() as session:
            agent = session.query(Agent).filter_by(repo_full_name=repo).first()
            if agent is not None and agent.built_runtime_image != runtime_image:
                agent.built_runtime_image = runtime_image
                session.commit()
    except Exception:  # noqa: BLE001
        log.warning("could not record the runtime %s now builds on", repo, exc_info=True)


def backfill_built_runtime(session: Session, github: GitHubClient) -> dict:
    """Fill `built_runtime_image` for every agent that has none yet, from
    its repository's Dockerfile. Idempotent: a filled row is never read
    again. An agent with no repository (a directory agent, or a row that
    failed before its repository existed) is left alone. A Dockerfile that
    cannot be read, or has no FROM line, is skipped and tried next boot.
    Never raises past a single agent."""
    rows = (
        session.query(Agent)
        .filter(Agent.built_runtime_image.is_(None), Agent.repo_full_name.isnot(None))
        .order_by(Agent.name)
        .all()
    )
    out = {"filled": 0, "skipped": 0, "no_repo": 0}
    for agent in rows:
        if agent.kind == KIND_DIRECTORY or not agent.repo_full_name:
            out["no_repo"] += 1
            continue
        try:
            text = github.read_file(agent.repo_full_name, DOCKERFILE)
        except Exception as exc:  # noqa: BLE001
            log.info("runtime backfill could not read %s for %s: %s", DOCKERFILE, agent.name, exc)
            out["skipped"] += 1
            continue
        runtime = dockerfile_runtime(text) if text is not None else None
        if runtime is None:
            out["skipped"] += 1
            continue
        agent.built_runtime_image = runtime
        session.commit()
        out["filled"] += 1
    return out


__all__ = ["backfill_built_runtime", "dockerfile_runtime", "record_built_runtime"]
