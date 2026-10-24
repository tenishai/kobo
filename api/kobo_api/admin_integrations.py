"""Connecting an integration from the admin panel (design 2026-10-18, Arc 2
step 3): one base credential per vendor, held once by the room, served to
agents through the tool gateway.

A connect does three things, in order, each visible on the card:

1. the row and its audit event (`admin_store.put_integration`), state
   `pending`;
2. the credential to the secrets add-on under the room's own path,
   `apps/kobo/integrations/<id>`, synced into the gateway's namespace as the
   Secret `integration-<id>`; the row keeps only the path (`credential_ref`);
3. a pull request on the gateway's config repository adding the vendor's
   `[[target]]` to `targets.toml`, naming the env variable the Secret must
   feed. The gateway serves the target after that pull request merges and
   the gateway rolls, so the row stays `pending` with the link until then.

Disconnect reverses the three. Rows are the truth; the vault and the repo
are projections, and `state_detail` says how far they have caught up.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from . import admin_store
from .agent_env import require_our_branch
from .clients import ClientError, Clients, GitHubClient
from .config import Config
from .models import AdminIntegration

log = logging.getLogger(__name__)

ROOM_APP = "kobo"
TARGETS_FILE = "targets.toml"
BRANCH_PREFIX = "kobo/integration-"
SECRET_KEY = "TOKEN"


class ConnectRefused(ValueError):
    """A connect the room cannot do yet, with the sentence that says why."""


def credential_path(integration_id: str) -> str:
    return f"apps/{ROOM_APP}/integrations/{integration_id}"


def secret_name(integration_id: str) -> str:
    return f"integration-{integration_id}"


def token_env(integration_id: str) -> str:
    return "INTEGRATION_" + re.sub(r"[^A-Za-z0-9]", "_", integration_id).upper() + "_" + SECRET_KEY


def target_stanza(row: AdminIntegration) -> str:
    """The `[[target]]` the gateway needs for this vendor: name, address,
    bearer token from the env the synced Secret feeds, trust tier. Scopes are
    the gateway operator's call and stay out of here."""
    spec = row.spec or {}
    address = spec.get("address", "")
    trust = spec.get("trust", "untrusted")
    return (
        f"\n# {row.name}: connected from the room\'s admin panel by {row.created_by}.\n"
        f"[[target]]\n"
        f'name = "{row.id}"\n'
        f'transport = "http"\n'
        f'address = "{address}"\n'
        f'trust = "{trust}"\n'
        f'auth_method = "bearer"\n'
        f'bearer_token_env = "{token_env(row.id)}"\n'
    )


def _without_stanza(text: str, integration_id: str) -> str:
    """`targets.toml` with this integration\'s stanza (and its comment) gone."""
    pattern = re.compile(
        r"\n?#[^\n]*connected from the room[^\n]*\n\[\[target\]\]\nname = \""
        + re.escape(integration_id)
        + r"\"\n(?:[a-z_]+ = [^\n]*\n)*",
    )
    return pattern.sub("", text)


def _gateway_branch_pr(github: GitHubClient, config: Config, *, branch: str, title: str, body: str, edit) -> str | None:
    """Clone the gateway repo, let `edit(workdir) -> bool` change it, and
    open (or find) the pull request on `branch`. None when no repository is
    configured or the edit changed nothing."""
    repo = config.gateway_config_repo
    if not repo:
        return None
    existing = github.find_pr(repo, branch=branch)
    if existing is not None:
        return existing
    with tempfile.TemporaryDirectory(prefix="kobo-gateway-") as tmp:
        workdir = Path(tmp) / "gateway"
        github.clone(repo, workdir)
        if not edit(workdir):
            return None
        if github.branch_exists(repo, branch):
            require_our_branch(github, repo, branch)
            github.delete_branch(repo, branch)
        github.commit_push(workdir, title)
        return github.open_pr(workdir, repo=repo, branch=branch, title=title, body=body)


def _gateway_pr(
    github: GitHubClient, config: Config, row: AdminIntegration, *, remove: bool, actor: str
) -> str | None:
    """Open (or find) the pull request that adds or removes this vendor's
    target. None when no repository is configured or nothing changed."""
    verb = "remove" if remove else "add"

    def edit(workdir: Path) -> bool:
        targets = workdir / TARGETS_FILE
        current = targets.read_text() if targets.exists() else ""
        stripped = _without_stanza(current, row.id)
        updated = stripped if remove else stripped.rstrip("\n") + "\n" + target_stanza(row)
        if updated == current:
            return False
        targets.write_text(updated)
        return True

    body = (
        f"Opened by the room\'s admin panel for {actor}.\n\n"
        + (
            f"Removes the `{row.id}` target."
            if remove
            else (
                f"Adds the `{row.id}` target. Its credential is the Secret `{secret_name(row.id)}` "
                f"in the `{config.gateway_namespace or '<gateway namespace>'}` namespace, synced by the "
                f"secrets add-on; the gateway\'s deployment must carry it as the env variable "
                f"`{token_env(row.id)}` (key `{SECRET_KEY}`). The target serves after this merges and "
                f"the gateway rolls."
            )
        )
    )
    return _gateway_branch_pr(
        github, config, branch=f"{BRANCH_PREFIX}{row.id}", title=f"kobo: {verb} the {row.name} target", body=body, edit=edit
    )

def connect(
    session: Session,
    clients: Clients | None,
    config: Config,
    *,
    vendor: str,
    name: str,
    base_access: str,
    credential: str,
    address: str,
    trust: str,
    actor: str,
) -> AdminIntegration:
    """The three steps above. The row is committed by the caller; a
    projection that fails leaves the row `pending` with the failure in
    `state_detail`, never an exception past the audit event."""
    sero = clients.sero if clients is not None else None
    if sero is None or not config.sero_url:
        raise ConnectRefused("the Secrets add-on is off, so the room has nowhere to hold the credential; turn it on first")
    if not credential.strip():
        raise ConnectRefused("an integration needs the vendor\'s token or key")
    spec: dict[str, Any] = {"address": address.strip(), "trust": trust if trust in ("trusted", "untrusted") else "untrusted"}
    row = admin_store.put_integration(
        session, vendor=vendor, name=name, base_access=base_access, spec=spec,
        credential_ref=None, actor=actor,
    )
    path = credential_path(row.id)
    try:
        sero.put_secret(actor, path, {SECRET_KEY: credential.strip()})
        if config.gateway_namespace:
            sero.ensure_synced(actor, ROOM_APP, path, config.gateway_namespace, secret_name(row.id))
        row.credential_ref = path
    except ClientError as exc:
        admin_store.set_integration_state(session, row.id, state=admin_store.STATE_FAILED, detail=f"the secrets add-on refused: {exc}")
        return row
    detail = "credential stored"
    if not config.gateway_namespace:
        detail += "; no gateway namespace is set, so it is not synced to the gateway yet"
    try:
        pr = _gateway_pr(clients.github, config, row, remove=False, actor=actor)
    except (ClientError, OSError) as exc:
        admin_store.set_integration_state(session, row.id, state=admin_store.STATE_FAILED, detail=f"{detail}; the gateway pull request failed: {exc}")
        return row
    if pr:
        detail += f"; pull request open on the gateway: {pr}; serves after it merges and the gateway rolls"
        row.spec = {**dict(row.spec or {}), "pull_request": pr}
    else:
        detail += "; no gateway config repository is set, so add the target by hand"
    admin_store.set_integration_state(session, row.id, state=admin_store.STATE_PENDING, detail=detail)
    return row


def disconnect(session: Session, clients: Clients | None, config: Config, *, integration_id: str, actor: str) -> dict[str, Any]:
    """Reverse the three steps. The row goes first (it is the truth); each
    projection\'s outcome is reported back as a sentence."""
    row = session.get(AdminIntegration, integration_id)
    if row is None:
        raise ConnectRefused(f"no integration {integration_id!r}")
    outcome: dict[str, Any] = {"id": integration_id, "secret": "left", "gateway": "left"}
    sero = clients.sero if clients is not None else None
    if sero is not None and row.credential_ref:
        try:
            if config.gateway_namespace:
                sero.unsync(actor, ROOM_APP, config.gateway_namespace, secret_name(integration_id))
            sero.delete_secret(actor, row.credential_ref)
            outcome["secret"] = "removed"
        except ClientError as exc:
            outcome["secret"] = f"left: {exc}"
    try:
        pr = _gateway_pr(clients.github, config, row, remove=True, actor=actor) if clients is not None else None
        outcome["gateway"] = f"pull request open: {pr}" if pr else "nothing to remove"
    except (ClientError, OSError) as exc:
        outcome["gateway"] = f"left: {exc}"
    admin_store.remove_integration(session, integration_id, actor=actor)
    return outcome
