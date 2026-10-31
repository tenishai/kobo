"""Wire existing agents to the tool gateway (Admin > Services, 2026-10-30).

The composer gives every new agent the gateway block when the org names its
gateway (`liminal_url`). An org that names it later has agents with no
provider for the tools their permissions unlock: a grant and a lane on the
gateway, and nothing in the agent to call them with. This module gives those
agents the block the composer would have: one commit on each agent's own
repository, which rebuilds and restarts it once.

Only the room's own agents (`composed`, READY, with a repository) qualify; a
connected agent's repository is its team's, and its wiring arrives by pull
request on another lane. A file that already carries the block is left alone
and reported as such. Best effort per agent: one refusal never stops the
rest, and every outcome is named in the answer and the audit row.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kobo_api.clients import ClientError
from kobo_api.composer import update_agent_yaml_gateway
from kobo_api.kinds import KIND_COMPOSED
from kobo_api.models import Agent, AgentStatus
from kobo_api.repo_lock import repo_write_lock

log = logging.getLogger(__name__)

COMMIT_MESSAGE = "kobo: wire the tool gateway"
NO_GATEWAY = "the org config names no tool gateway (liminal_url); set it and reload the org config first"
NO_SWITCHBOARD = "the gateway's token exchange rides the switchboard (cabal_url), which the org config does not name"


def candidates(session: Session, names: list[str] | None = None) -> list[Agent]:
    """The room's own READY agents with a repository, in name order; `names`
    narrows to those (unknown names are simply absent)."""
    rows = [
        a for a in session.query(Agent).order_by(Agent.name).all()
        if a.kind == KIND_COMPOSED and a.status == AgentStatus.READY and a.repo_full_name
    ]
    if names:
        wanted = set(names)
        rows = [a for a in rows if a.name in wanted]
    return rows


def preview(session: Session, config, names: list[str] | None = None) -> dict[str, Any]:
    """What a wire would touch. Whether each file already carries the block is
    only known once its repository is read, so the preview names the agents
    and the wire reports already/wired per agent."""
    note = None
    if not getattr(config, "liminal_url", ""):
        note = NO_GATEWAY
    elif not getattr(config, "cabal_url", ""):
        note = NO_SWITCHBOARD
    return {"agents": [a.name for a in candidates(session, names)], "gateway": getattr(config, "liminal_url", "") or None, "note": note}


def wire(session: Session, clients, config, *, names: list[str] | None = None) -> dict[str, Any]:
    """Give every candidate the gateway block. Returns `wired` ({name, sha}),
    `already` (names), `failed` ({name: sentence}) and the preview's `note`
    when nothing could be done at all."""
    out: dict[str, Any] = {"wired": [], "already": [], "failed": {}, "note": None}
    pre = preview(session, config, names)
    if pre["note"]:
        out["note"] = pre["note"]
        return out
    github = getattr(clients, "github", None) if clients is not None else None
    if github is None:
        out["note"] = "this room has no GitHub client to write an agent's repository with"
        return out
    for agent in candidates(session, names):
        repo = agent.repo_full_name
        try:
            with repo_write_lock(repo), tempfile.TemporaryDirectory(prefix="kobo-gateway-wire-") as tmp:
                workdir = Path(tmp) / "repo"
                github.clone(repo, workdir)
                changed = update_agent_yaml_gateway(
                    workdir / "bundle", url=config.liminal_url, exchange_url=config.cabal_url,
                )
                if not changed:
                    out["already"].append(agent.name)
                    continue
                github.commit_push(workdir, COMMIT_MESSAGE)
                sha = github.local_head(workdir)
            out["wired"].append({"name": agent.name, "sha": sha})
            log.info("gateway wiring pushed for %s (%s)", agent.name, sha)
        except (ClientError, OSError) as exc:
            out["failed"][agent.name] = str(exc)
            log.warning("gateway wiring for %s failed: %s", agent.name, exc)
    return out


__all__ = ["COMMIT_MESSAGE", "NO_GATEWAY", "NO_SWITCHBOARD", "candidates", "preview", "wire"]
