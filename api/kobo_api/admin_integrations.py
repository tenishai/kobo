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
from . import bab as bab_mod
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
    "aws_role": "an IAM role the room assumes directly",
    # Hub and spoke (permission service on): one hub role per AWS organization, in its
    # management account, that the room assumes; one spoke role per member account that
    # the hub assumes with an external id. Never an assume across organizations.
    "aws_hub": "an AWS organization: the hub role in its management account",
    "aws_spoke": "an AWS account under a connected organization: the role its hub assumes",
    "gcp_identity": "workload identity, optionally impersonating a service account",
    "azure_federated": "an app registration federated to the gateway's identity",
}
_AWS_ACCOUNT_ID = re.compile(r"^\d{12}$")
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
        raise ConnectRefused("AWS needs the ARN of a role the room may assume (arn:aws:iam::<account>:role/<name>)")
    if auth == "aws_hub" and not _AWS_ROLE_ARN.match(identity.get("hub_role_arn", "").strip()):
        raise ConnectRefused("an AWS organization needs the ARN of its hub role, in the management account")
    if auth == "aws_spoke":
        if not identity.get("parent", "").strip():
            raise ConnectRefused("an AWS account connects under one of the room's connected organizations")
        if not _AWS_ROLE_ARN.match(identity.get("role_arn", "").strip()):
            raise ConnectRefused("an AWS account needs the ARN of the role its organization's hub assumes")
        if not _AWS_ACCOUNT_ID.match(identity.get("account_id", "").strip()):
            raise ConnectRefused("an AWS account needs its 12-digit account id")
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


def _bab(clients: Clients | None):
    return getattr(clients, "bab", None) if clients is not None else None


def _org_or_refused(bab, parent: str, actor: str) -> dict:
    """The connected organization an account hangs under, with its hub role."""
    for i in bab.integrations(actor):
        if i["integration"] == parent and i.get("kind") == "org":
            return i
    raise ConnectRefused(f"{parent!r} is not one of the room's connected AWS organizations")


def _org_hub(bab, parent: str, actor: str) -> str:
    """The hub role ARN of a connected organization: bab keeps it in the row's spec, which
    its listing does not return, so the console asks the probe-free way: the org row's
    accounts call fails without it, and the setup page needs it for the spoke's trust."""
    _org_or_refused(bab, parent, actor)
    return str(bab.org_hub_role_arn(parent, actor))


def aws_setup(clients: Clients | None, *, integration_id: str, packs: list[str] | None, actor: str = "operator@room",
              mode: str = "role", parent: str | None = None, account_id: str | None = None) -> dict[str, Any]:
    """Everything the Connect AWS page shows: who the role must trust, which
    packs exist, and the three ways to create the role, each pre-filled. With
    the permission service on, the principal is bab (it mints the leases); the
    gateway otherwise.

    Three modes. `role`: one flat role the room assumes (the gateway path, or
    bab without an organization). `org`: the hub role in an organization's
    management account, trusting the room's identity. `account`: a member
    account's spoke role under a connected organization, trusting the hub
    with this account's external id."""
    bab = _bab(clients)
    if mode == "account":
        if bab is None:
            raise ConnectRefused("accounts under an organization need the permission service")
        if not parent or not account_id or not _AWS_ACCOUNT_ID.match(account_id):
            raise ConnectRefused("an account set-up needs the organization and the 12-digit account id")
        hub = _org_hub(bab, parent, actor)
        external_id = bab.external_id_for_org(parent)
        chosen = cloud_access.chosen_packs(packs)
        return {
            "version": cloud_access.version(), "mode": "account", "principal": "access",
            "role_name": cloud_access.ROLE_NAME, "audience": cloud_access.AUDIENCE,
            "parent": parent, "account_id": account_id, "hub_role_arn": hub, "external_id": external_id,
            "identity": None, "identity_note": None,
            "packs": cloud_access.aws_packs(), "chosen": chosen,
            "terraform": cloud_access.spoke_terraform_snippet(hub, external_id, integration_id, chosen),
            "cloudformation": {"template": cloud_access.cloudformation_template(),
                               "parameters": {**cloud_access.cloudformation_parameters("", integration_id, chosen),
                                              "HubRoleArn": hub, "ExternalId": external_id}},
            "trust_policy": cloud_access.spoke_trust_policy(hub, external_id),
            "checked": True,
        }
    admin = getattr(clients, "liminal_admin", None) if clients is not None else None
    identity: dict[str, str] | None = None
    identity_note: str | None = None
    principal = "access" if bab is not None else "gateway"
    if bab is not None:
        try:
            identity = bab.aws_identity(actor)
        except ClientError as exc:
            identity_note = str(exc)
    elif admin is None:
        identity_note = "the gateway\'s admin plane is not wired, so the room cannot read its identity; fill the subject in by hand"
    else:
        try:
            identity = admin.aws_identity(cloud_access.AUDIENCE)
        except ClientError as exc:
            identity_note = str(exc)
    subject = (identity or {}).get("subject", "")
    chosen = cloud_access.chosen_packs(packs)
    if mode == "org":
        if bab is None:
            raise ConnectRefused("organizations need the permission service")
        # The member accounts' half (#275): one external id for the whole organization and
        # one spoke template, so the role is deployed once (a StackSet) and the hub then
        # finds every account that has it. The hub ARN is known once the organization is
        # connected; before that the snippets carry a placeholder the owner fills in.
        external_id = bab.external_id_for_org(integration_id)
        try:
            hub = _org_hub(bab, integration_id, actor)
        except (ConnectRefused, ClientError):
            hub = ""
        hub_shown = hub or f"arn:aws:iam::<management account>:role/{cloud_access.HUB_ROLE_NAME}"
        return {
            "version": cloud_access.version(), "mode": "org", "principal": "access",
            "role_name": cloud_access.HUB_ROLE_NAME, "audience": cloud_access.AUDIENCE,
            "identity": identity, "identity_note": identity_note,
            "packs": cloud_access.aws_packs(), "chosen": chosen,
            "terraform": cloud_access.hub_terraform_snippet(subject, integration_id),
            "cloudformation": {"template": cloud_access.cloudformation_template(),
                               "parameters": {**cloud_access.cloudformation_parameters("", integration_id, chosen),
                                              "HubRoleArn": hub_shown, "ExternalId": external_id}},
            "trust_policy": cloud_access.hub_trust_policy(subject),
            "permissions_policy": cloud_access.hub_permissions_policy(),
            "external_id": external_id,
            "hub_role_arn": hub or None,
            "spoke_role_name": cloud_access.ROLE_NAME,
            "spoke_terraform": cloud_access.spoke_terraform_snippet(hub_shown, external_id, integration_id, chosen),
            "spoke_trust_policy": cloud_access.spoke_trust_policy(hub_shown, external_id),
            "stackset": cloud_access.stackset_snippet(hub_shown, external_id, integration_id, chosen),
            "checked": True,
        }
    return {
        "version": cloud_access.version(),
        "mode": "role",
        "role_name": cloud_access.ROLE_NAME,
        "audience": cloud_access.AUDIENCE,
        "principal": principal,
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
        "checked": bab is not None or admin is not None,
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
    bab = _bab(clients)
    probe: dict[str, str] | None = None
    hub_role_arn: str | None = None
    if auth in ("aws_hub", "aws_spoke") and bab is None:
        raise ConnectRefused("AWS organizations and their accounts need the permission service; turn Access on first")
    if auth == "aws_role":
        identity.setdefault("audience", cloud_access.AUDIENCE)
        if bab is not None:
            try:
                probe = bab.probe_aws(identity["role_arn"], identity["audience"], actor)
            except ClientError as exc:
                raise ConnectRefused(f"{exc}; fix the role\'s trust policy and connect again") from exc
        else:
            probe = probe_aws_role(clients, identity["role_arn"], identity["audience"])
    elif auth == "aws_hub":
        identity.setdefault("audience", cloud_access.AUDIENCE)
        try:
            probe = bab.probe_aws(identity["hub_role_arn"], identity["audience"], actor)
        except ClientError as exc:
            raise ConnectRefused(f"{exc}; fix the hub role\'s trust policy and connect again") from exc
    elif auth == "aws_spoke":
        hub_role_arn = _org_hub(bab, identity["parent"], actor)
        identity["external_id"] = bab.external_id_for_org(identity["parent"])
        identity.setdefault("audience", cloud_access.AUDIENCE)
        try:
            probe = bab.probe_aws(identity["role_arn"], identity["audience"], actor,
                                  hub_role_arn=hub_role_arn, external_id=identity["external_id"])
        except ClientError as exc:
            raise ConnectRefused(f"{exc}; the account\'s role must trust the organization\'s hub with this external id; "
                                 "fix it and connect again") from exc
    if secrets and (sero is None or not config.sero_url):
        raise ConnectRefused("the Secrets add-on is off, so the room has nowhere to hold the credential; turn it on first")
    spec: dict[str, Any] = {
        "address": address.strip(),
        "trust": trust if trust in ("trusted", "untrusted") else "untrusted",
        "auth": auth,
        "identity": {k: v for k, v in identity.items() if k != "external_id"},
        "secret_keys": sorted(secrets),
    }
    if auth == "aws_hub":
        spec["kind"] = "org"
    elif auth == "aws_spoke":
        spec["kind"], spec["parent"], spec["account_id"] = "account", identity["parent"], identity["account_id"]
    # Organizations and their accounts are named by the person (acme-aws, acme-prod), so the
    # row id is the name, not the vendor: a vendor may hold many of them.
    wanted_id = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:64] if auth in ("aws_hub", "aws_spoke") else None
    row = admin_store.put_integration(
        session, vendor=vendor, name=name, base_access=base_access, spec=spec,
        credential_ref=None, actor=actor, integration_id=wanted_id,
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
    detail = "credential stored" if secrets else "no secret to store; the connection uses the room\'s own identity"
    minter = "the permission service" if bab is not None else "the gateway"
    if probe is not None:
        detail = f"AWS agreed: {minter} assumed the role in account {probe['account']}"
    elif auth == "aws_role":
        detail = "not checked with AWS: the gateway\'s admin plane is not wired; the role is taken at its word"
    if secrets and not config.gateway_namespace:
        detail += "; no gateway namespace is set, so it is not synced to the gateway yet"
    # The permission service holds the connection: what agents may be granted there (the
    # ceiling is every read permission the catalogue offers for the vendor; the role's or
    # token's own policy is the outer wall) and, for a cloud, what to assume.
    if bab is not None:
        parent_id: str | None = None
        if auth == "aws_role":
            spec_for_bab: dict[str, Any] = {"role_arn": identity["role_arn"], "audience": identity["audience"], "name": name}
            ceiling = bab_mod.read_permissions(bab, vendor, actor)
        elif auth == "aws_hub":
            spec_for_bab = {"hub_role_arn": identity["hub_role_arn"], "audience": identity["audience"], "name": name,
                            "external_id": bab.external_id_for_org(wanted_id or name)}
            ceiling = []  # nothing is granted on an organization; its accounts carry the ceilings
        elif auth == "aws_spoke":
            parent_id = identity["parent"]
            spec_for_bab = {"spoke_role_arn": identity["role_arn"], "external_id": identity["external_id"],
                            "account_id": identity["account_id"], "audience": identity["audience"], "name": name}
            ceiling = bab_mod.read_permissions(bab, vendor, actor)
        else:
            spec_for_bab, ceiling = {}, bab_mod.read_permissions(bab, vendor, actor)
        try:
            bab.put_integration(row.id, vendor, spec_for_bab, ceiling, actor, parent=parent_id)
            detail += ("; connected in the permission service as an organization"
                       if auth == "aws_hub" else "; connected in the permission service, agents may ask for its read permissions")
        except ClientError as exc:
            admin_store.set_integration_state(session, row.id, state=admin_store.STATE_FAILED, detail=f"{detail}; the permission service refused: {exc}")
            return row
        if auth == "aws_hub":
            # The accounts register themselves (#275): every one the hub can reach with the
            # organization's external id is connected now; the rest wait for the role.
            try:
                found = register_accounts(session, clients, config, org_id=row.id, actor=actor)
                detail += (f"; {len(found['connected'])} account(s) carry the role and are connected, "
                           f"{len(found['without_role'])} do not yet")
            except ClientError as exc:
                detail += f"; its accounts could not be checked yet: {exc}"
    if bab is not None and auth in ("aws_role", "aws_hub", "aws_spoke"):
        # Leases come from the permission service; the gateway has nothing to learn.
        admin_store.set_integration_state(session, row.id, state=admin_store.STATE_APPLIED, detail=detail)
        return row
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


def register_accounts(session: Session, clients: Clients | None, config: Config, *, org_id: str, actor: str) -> dict[str, Any]:
    """Ask the permission service which of an organization's accounts carry the spoke
    role (probed through the hub with the organization's one external id) and connect
    the ones that do: a row here and an account under the organization there, with the
    read ceiling. Accounts without the role are reported, not created. Safe to run again:
    connected accounts are left alone."""
    bab = _bab(clients)
    if bab is None:
        raise ConnectRefused("checking an organization\'s accounts needs the permission service")
    org = _org_or_refused(bab, org_id, actor)
    # An organization connected before external ids were per organization has none in
    # the permission service; give it its id now (the same derivation the set-up page
    # shows), so discover can probe its accounts. Its spoke rows keep their own ids.
    if not (org.get("external_id") or (org.get("spec") or {}).get("external_id")):
        bab.put_integration(org_id, "aws", {"hub_role_arn": org.get("hub_role_arn") or bab.org_hub_role_arn(org_id, actor),
                                            "audience": cloud_access.AUDIENCE, "name": org.get("name") or org_id,
                                            "external_id": bab.external_id_for_org(org_id)}, [], actor)
    found = bab.discover_accounts(org_id, actor, cloud_access.ROLE_NAME)
    taken = {r.id for r in admin_store.list_integrations(session)} | {i["integration"] for i in bab.integrations(actor)}
    ceiling: list[str] | None = None
    out: dict[str, Any] = {"organization": org_id, "connected": [], "without_role": [], "already": []}
    for a in found.get("accounts", []):
        entry = {"id": a["id"], "name": a.get("name") or a["id"], "integration": a.get("integration"), "reason": a.get("reason")}
        if a.get("connected"):
            out["already"].append(entry)
            continue
        if not a.get("reachable"):
            out["without_role"].append(entry)
            continue
        base = re.sub(r"[^a-z0-9-]", "-", str(entry["name"]).lower()).strip("-")[:60] or a["id"]
        row_id = base if base not in taken else f"{base}-{a['id'][-4:]}"
        taken.add(row_id)
        if ceiling is None:
            ceiling = bab_mod.read_permissions(bab, "aws", actor)
        spec_for_bab = {"spoke_role_arn": a["role_arn"], "account_id": a["id"], "audience": cloud_access.AUDIENCE, "name": entry["name"]}
        bab.put_integration(row_id, "aws", spec_for_bab, ceiling, actor, parent=org_id)
        row = admin_store.put_integration(
            session, vendor="aws", name=str(entry["name"]), base_access="read",
            spec={"address": "", "trust": "untrusted", "auth": "aws_spoke", "kind": "account", "parent": org_id,
                  "account_id": a["id"], "identity": {"parent": org_id, "role_arn": a["role_arn"], "account_id": a["id"],
                                                      "audience": cloud_access.AUDIENCE}, "secret_keys": []},
            credential_ref=None, actor=actor, integration_id=row_id,
        )
        admin_store.set_integration_state(
            session, row.id, state=admin_store.STATE_APPLIED,
            detail="found through the organization\'s hub: the account carries the role; connected in the permission service, "
                   "agents may ask for its read permissions")
        entry["integration"] = row.id
        out["connected"].append(entry)
    return out


def disconnect(session: Session, clients: Clients | None, config: Config, *, integration_id: str, actor: str) -> dict[str, Any]:
    """Reverse the three steps. The row goes first (it is the truth); each
    projection\'s outcome is reported back as a sentence."""
    bab = _bab(clients)
    row = session.get(AdminIntegration, integration_id)
    if row is None:
        # Connected straight into the permission service (never through this panel): take it out of there.
        if bab is not None:
            try:
                out = bab.delete_integration(integration_id, actor)
            except ClientError as exc:
                raise ConnectRefused(str(exc)) from exc
            return {"id": integration_id, "secret": "none", "gateway": "none",
                    "access": f"removed; {out.get('grants_revoked', 0)} grant(s) revoked"}
        raise ConnectRefused(f"no integration {integration_id!r}")
    outcome: dict[str, Any] = {"id": integration_id, "secret": "left", "gateway": "left"}
    if bab is not None:
        try:
            out = bab.delete_integration(integration_id, actor)
            outcome["access"] = f"removed; {out.get('grants_revoked', 0)} grant(s) revoked"
        except ClientError as exc:
            outcome["access"] = f"left: {exc}"
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
