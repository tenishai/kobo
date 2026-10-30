"""A row that waits on a gateway pull request learns how it ended (kobo#327).

An integration connected through the admin panel is `pending` while the pull
request the room opened on the gateway's config repository is open; nothing
else moves it. This reads each such pull request's state when the Integrations
pane loads: merged means the gateway carries the change once it rolls, so the
row is Connected and the link is dropped; closed without merging means
somebody declined it, so the row is Failed and says so. Best effort, never
raises, one GitHub read per waiting row.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from . import admin_store

log = logging.getLogger(__name__)

MERGED = "the pull request merged; the gateway serves the change once it rolls"
CLOSED = "the pull request was closed without merging; remove this row and add it again to open a new one"


def reconcile(session: Session, clients, config) -> int:
    """Settle every pending row whose pull request has ended. Returns how many rows changed."""
    github = getattr(clients, "github", None) if clients is not None else None
    repo = getattr(config, "gateway_config_repo", "")
    if github is None or not repo:
        return 0
    changed = 0
    for row in admin_store.list_integrations(session):
        url = (row.spec or {}).get("pull_request")
        if row.state != admin_store.STATE_PENDING or not url:
            continue
        state = github.pr_state(repo, url)
        if state == "open" or state == "unknown":
            continue
        spec = {k: v for k, v in dict(row.spec or {}).items() if k != "pull_request"}
        spec["last_pull_request"] = url
        row.spec = spec
        head = (row.state_detail or "").split("; pull request open", 1)[0]
        if state == "merged":
            admin_store.set_integration_state(session, row.id, state=admin_store.STATE_APPLIED, detail=f"{head}; {MERGED}")
        else:
            admin_store.set_integration_state(session, row.id, state=admin_store.STATE_FAILED, detail=f"{head}; {CLOSED}: {url}")
        changed += 1
    if changed:
        session.commit()
    return changed
