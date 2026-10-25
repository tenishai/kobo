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

from . import admin_store, cloud_access
from .agent_env import require_our_branch
from .clients import ClientError, Clients, GitHubClient
from .config import Config
from .models import AdminIntegration

log = logging.getLogger(__name__)

ROOM_APP = "kobo"
TARGETS_FILE = "targets.toml"
# An AWS account is operator config for the gateway's `aws` handler, not a
# target: `[tool_extras."aws.roles".<id>]` in the trust registry.
REGISTRY_FILE = "registry.toml"
AWS_ROLES_TABLE = "aws.roles"
BRANCH_PREFIX = "kobo/integration-"
SECRET_KEY = "TOKEN"

# How the gateway reaches a vendor (Kyle, 2026-10-25: AWS is a role, not a
# key; GCP is workload identity or a scoped service account; the API-token
# vendors are tokens). `identity` fields are plain values the gateway reads
# from its config; `secrets` are stored in the secrets service and reach the
# gateway as env vars named `secret_env(id, key)`. A kind with no secret
# stores nothing: the gateway's own cloud identity does the work.
AUTH_KINDS = {
    "token": "one bearer token",
    "api_keys": "one or more named keys",
    "aws_role": "an IAM role the gateway assumes",
    "gcp_identity": "workload identity, optionally impersonating a service account",
    "azure_federated": "an app registration federated to the gateway's identity",
}
_AWS_ROLE_ARN = re.compile(r"^arn:aws:iam::\d{12}:role/[\w+=,.@/-]+$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GUID = re.compile(r"^[0-9a-fA-F-]{32,36}$")


def check_auth(auth: str, identity: dict[str, str], secrets: dict[str, str]) -> None:
    """Refuse a connect whose fields do not fit its kind, in one sentence."""
    if auth not in AUTH_KINDS:
        raise ConnectRefused(f"unknown auth kind {auth!r}; one of {', '.join(AUTH_KINDS)}")
    if auth == "token" and not secrets.get(SECRET_KEY, "").strip():
        raise ConnectRefused("an integration needs the vendor's token or key")
    if auth == "api_keys" and not any(v.strip() for v in secrets.values()):
        raise ConnectRefused("this vendor needs at least one key")
    if auth == "aws_role" and not _AWS_ROLE_ARN.match(identity.get("role_arn", "").strip()):
        raise ConnectRefused("AWS needs the ARN of a role the gateway may assume (arn:aws:iam::<account>:role/<name>)")
    if auth == "gcp_identity":
        sa = identity.get("service_account", "").strip()
        if sa and not _EMAIL.match(sa):
            raise ConnectRefused("the service account to act as must be its email address")
    if auth == "azure_federated" and not (
        _GUID.match(identity.get("client_id", "").strip()) and _GUID.match(identity.get("tenant_id", "").strip())
    ):
        raise ConnectRefused("Azure needs the app registration's client ID and tenant ID")


class ConnectRefused(ValueError):
    """A connect the room cannot do yet, with the sentence that says why."""


def credential_path(integration_id: str) -> str:
    return f"apps/{ROOM_APP}/integrations/{integration_id}"


def secret_name(integration_id: str) -> str:
    return f"integration-{integration_id}"


def secret_env(integration_id: str, key: str = SECRET_KEY) -> str:
    """The env var one stored secret reaches the gateway as."""
    return "INTEGRATION_" + re.sub(r"[^A-Za-z0-9]", "_", integration_id).upper() + "_" + re.sub(r"[^A-Za-z0-9]", "_", key).upper()


def token_env(integration_id: str) -> str:
    return secret_env(integration_id, SECRET_KEY)


def target_stanza(row: AdminIntegration) -> str:
    """The `[[target]]` the gateway needs for this vendor: name, address,
    bearer token from the env the synced Secret feeds, trust tier. Scopes are
    the gateway operator's call and stay out of here."""
    spec = row.spec or {}
    address = spec.get("address", "")
    trust = spec.get("trust", "untrusted")
    auth = spec.get("auth", "token")
    head = (
        f"\n# {row.name}: connected from the room\'s admin panel by {row.created_by}.\n"
        f"[[target]]\n"
        f'name = "{row.id}"\n'
        f'transport = "http"\n'
        f'address = "{address}"\n'
        f'trust = "{trust}"\n'
    )
    if auth == "token":
        return head + f'auth_method = "bearer"\nbearer_token_env = "{token_env(row.id)}"\n'
    # Every other kind: the method named, its plain fields, and an env name
    # per stored secret, under the target's own auth table.
    lines = [head + f'auth_method = "{auth}"\n', "[target.auth]\n"]
    for key, value in sorted((spec.get("identity") or {}).items()):
        lines.append(f'{key} = "{value}"\n')
    for key in sorted(spec.get("secret_keys") or []):
        lines.append(f'{key.lower()}_env = "{secret_env(row.id, key)}"\n')
    return "".join(lines)


def aws_roles_stanza(row: AdminIntegration) -> str:
    """The trust-registry table the gateway's `aws` handler reads for this
    account: the role to assume and the audience its trust policy expects."""
    identity = (row.spec or {}).get("identity") or {}
    lines = [
        f"\n# {row.name}: connected from the room\'s admin panel by {row.created_by}.\n",
        f'[tool_extras."{AWS_ROLES_TABLE}".{row.id}]\n',
        f'role_arn = "{identity.get("role_arn", "")}"\n',
    ]
    if identity.get("audience"):
        lines.append(f'audience = "{identity["audience"]}"\n')
    return "".join(lines)


def _without_aws_stanza(text: str, integration_id: str) -> str:
    pattern = re.compile(
        r"\n?#[^\n]*connected from the room[^\n]*\n\[tool_extras\.\"" + re.escape(AWS_ROLES_TABLE) + r"\"\."
        + re.escape(integration_id)
        + r"\]\n(?:[a-z_]+ = [^\n]*\n)*",
    )
    return pattern.sub("", text)


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

    aws = (row.spec or {}).get("auth") == "aws_role"

    def edit(workdir: Path) -> bool:
        path = workdir / (REGISTRY_FILE if aws else TARGETS_FILE)
        current = path.read_text() if path.exists() else ""
        if aws:
            stripped = _without_aws_stanza(current, row.id)
            updated = stripped if remove else stripped.rstrip("\n") + "\n" + aws_roles_stanza(row)
        else:
            stripped = _without_stanza(current, row.id)
            updated = stripped if remove else stripped.rstrip("\n") + "\n" + target_stanza(row)
        if updated == current:
            return False
        path.write_text(updated)
        return True

    if aws:
        what = "account"
        added = (
            f"Adds the `{row.id}` account to `[tool_extras.\"{AWS_ROLES_TABLE}\"]` in the trust registry. "
            f"The gateway\'s `aws` handler assumes the role with its own identity, one session per agent, "
            f"once a lane grants `{row.id}:read`. No secret is involved. Serves after this merges and the gateway rolls."
        )
    else:
        what = "target"
        added = (
            f"Adds the `{row.id}` target. Its credential is the Secret `{secret_name(row.id)}` "
            f"in the `{config.gateway_namespace or '<gateway namespace>'}` namespace, synced by the "
            f"secrets add-on; the gateway\'s deployment must carry it as the env variable "
            f"`{token_env(row.id)}` (key `{SECRET_KEY}`). The target serves after this merges and "
            f"the gateway rolls."
        )
    body = f"Opened by the room\'s admin panel for {actor}.\n\n" + (f"Removes the `{row.id}` {what}." if remove else added)
    return _gateway_branch_pr(
        github, config, branch=f"{BRANCH_PREFIX}{row.id}", title=f"kobo: {verb} the {row.name} {what}", body=body, edit=edit
    )

def probe_aws_role(clients: Clients | None, role_arn: str, audience: str) -> dict[str, str] | None:
    """Ask the gateway to assume the role once. None when no admin plane is
    wired (the room cannot check); `ConnectRefused` with AWS\'s sentence when
    it is and AWS said no, so nothing is written for a role that does not work."""
    admin = getattr(clients, "liminal_admin", None) if clients is not None else None
    if admin is None:
        return None
    try:
        return admin.aws_probe(role_arn, audience)
    except ClientError as exc:
        raise ConnectRefused(f"{exc}; fix the role\'s trust policy and connect again") from exc


def aws_setup(clients: Clients | None, *, integration_id: str, packs: list[str] | None) -> dict[str, Any]:
    """Everything the Connect AWS page shows: who the gateway is, which packs
    exist, and the three ways to create the role, each pre-filled."""
    admin = getattr(clients, "liminal_admin", None) if clients is not None else None
    identity: dict[str, str] | None = None
    identity_note: str | None = None
    if admin is None:
        identity_note = "the gateway\'s admin plane is not wired, so the room cannot read its identity; fill the subject in by hand"
    else:
        try:
            identity = admin.aws_identity(cloud_access.AUDIENCE)
        except ClientError as exc:
            identity_note = str(exc)
    subject = (identity or {}).get("subject", "")
    chosen = cloud_access.chosen_packs(packs)
    return {
        "version": cloud_access.version(),
        "role_name": cloud_access.ROLE_NAME,
        "audience": cloud_access.AUDIENCE,
        "identity": identity,
        "identity_note": identity_note,
        "packs": cloud_access.aws_packs(),
        "chosen": chosen,
        "terraform": cloud_access.terraform_snippet(subject, integration_id, chosen),
        "cloudformation": {
            "template": cloud_access.cloudformation_template(),
            "parameters": cloud_access.cloudformation_parameters(subject, integration_id, chosen),
        },
        "trust_policy": cloud_access.trust_policy(subject),
        "checked": admin is not None,
    }


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
    auth: str = "token",
    identity: dict[str, str] | None = None,
    secrets: dict[str, str] | None = None,
) -> AdminIntegration:
    """The three steps above. The row is committed by the caller; a
    projection that fails leaves the row `pending` with the failure in
    `state_detail`, never an exception past the audit event."""
    sero = clients.sero if clients is not None else None
    identity = {k: v.strip() for k, v in (identity or {}).items() if v and v.strip()}
    secrets = {k.upper(): v for k, v in (secrets or {}).items() if v and v.strip()}
    if credential.strip() and SECRET_KEY not in secrets:
        secrets[SECRET_KEY] = credential.strip()
    check_auth(auth, identity, secrets)
    probe: dict[str, str] | None = None
    if auth == "aws_role":
        identity.setdefault("audience", cloud_access.AUDIENCE)
        probe = probe_aws_role(clients, identity["role_arn"], identity["audience"])
    if secrets and (sero is None or not config.sero_url):
        raise ConnectRefused("the Secrets add-on is off, so the room has nowhere to hold the credential; turn it on first")
    spec: dict[str, Any] = {
        "address": address.strip(),
        "trust": trust if trust in ("trusted", "untrusted") else "untrusted",
        "auth": auth,
        "identity": identity,
        "secret_keys": sorted(secrets),
    }
    row = admin_store.put_integration(
        session, vendor=vendor, name=name, base_access=base_access, spec=spec,
        credential_ref=None, actor=actor,
    )
    path = credential_path(row.id)
    try:
        if secrets:
            sero.put_secret(actor, path, dict(secrets))
            if config.gateway_namespace:
                sero.ensure_synced(actor, ROOM_APP, path, config.gateway_namespace, secret_name(row.id))
            row.credential_ref = path
    except ClientError as exc:
        admin_store.set_integration_state(session, row.id, state=admin_store.STATE_FAILED, detail=f"the secrets add-on refused: {exc}")
        return row
    detail = "credential stored" if secrets else "no secret to store; the gateway uses its own identity"
    if probe is not None:
        detail = f"AWS agreed: the gateway assumed the role in account {probe['account']}"
    elif auth == "aws_role":
        detail = "not checked with AWS: the gateway\'s admin plane is not wired; the role is taken at its word"
    if secrets and not config.gateway_namespace:
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
