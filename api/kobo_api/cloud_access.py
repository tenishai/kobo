"""The cloud-access release the room ships: what an account owner gets on the
Connect AWS page (design 2026-10-25, cloud access for integrations).

`tenishai/cloud-access` is private while the suite is, so the room does not
link to it: a pinned release is vendored under `vendor/cloud_access/` and the
page is rendered from those bytes. The packs file is the contract (which
read-only access each pack is); the CloudFormation template is served as is;
the Terraform snippet points at the module source so an owner whose
organisation can read the repo applies it from there.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_DIR = Path(__file__).parent / "vendor" / "cloud_access"
MODULE_SOURCE = "git::https://github.com/tenishai/cloud-access.git//aws/terraform"
HUB_MODULE_SOURCE = "git::https://github.com/tenishai/cloud-access.git//aws/hub"
HUB_ROLE_NAME = "KoboAccess-Hub"
ROLE_NAME = "KoboAccess-Role"
AUDIENCE = "sts.amazonaws.com"
GOOGLE_ISSUER = "accounts.google.com"
REQUIRED_PACK = "inventory"


@lru_cache(maxsize=1)
def version() -> str:
    """`v0.1.0 f8951a8`: the tag and the commit the vendored files came from."""
    return (_DIR / "VERSION").read_text().strip()


@lru_cache(maxsize=1)
def aws_packs() -> list[dict[str, Any]]:
    """One entry per pack: id, purpose, required, and what it grants, in words."""
    raw = json.loads((_DIR / "packs-aws.json").read_text())
    out = []
    for p in raw["packs"]:
        grants = list(p.get("managed_policies") or [])
        grants = [g.rsplit("/", 1)[-1] for g in grants]
        for s in p.get("statements") or []:
            actions = s.get("Action") or []
            grants.append(f"{len(actions)} {s.get('Sid', 'inline')} actions")
        out.append({
            "id": p["id"], "purpose": p.get("purpose", ""), "required": bool(p.get("required")),
            "grants": grants,
        })
    return out


def chosen_packs(wanted: list[str] | None) -> list[str]:
    known = [p["id"] for p in aws_packs()]
    picked = [p for p in (wanted or []) if p in known]
    return [REQUIRED_PACK] + [p for p in known if p in picked and p != REQUIRED_PACK]


@lru_cache(maxsize=1)
def cloudformation_template() -> str:
    return (_DIR / "kobo-access.json").read_text()


def terraform_snippet(subject: str, integration_id: str, packs: list[str], audience: str = AUDIENCE) -> str:
    tag = version().split()[0]
    pack_list = ", ".join(f'"{p}"' for p in packs)
    return (
        f'module "kobo_access" {{\n'
        f'  source = "{MODULE_SOURCE}?ref={tag}"\n\n'
        f'  integration_id                    = "{integration_id}"\n'
        f'  gateway_service_account_unique_id = "{subject or "<gateway subject>"}"\n'
        f'  audience                          = "{audience}"\n'
        f"  packs                             = [{pack_list}]\n"
        f"}}\n\n"
        f'output "kobo_role_arn" {{\n  value = module.kobo_access.role_arn\n}}\n'
    )


def trust_policy(subject: str, audience: str = AUDIENCE) -> str:
    """For an owner who creates the role by hand: the trust policy alone. The audience
    condition is `oaud`: AWS maps a Google ID token's `aud` claim to `oaud` (and its `azp`
    to `aud`), so a policy written on `aud` refuses the token a service account mints."""
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "RoomOIDC",
            "Effect": "Allow",
            "Principal": {"Federated": GOOGLE_ISSUER},
            "Action": ["sts:AssumeRoleWithWebIdentity", "sts:TagSession", "sts:SetSourceIdentity"],
            "Condition": {"StringEquals": {
                f"{GOOGLE_ISSUER}:sub": subject or "<room subject>",
                f"{GOOGLE_ISSUER}:oaud": audience,
            }},
        }],
    }, indent=2)


def hub_terraform_snippet(subject: str, integration_id: str, audience: str = AUDIENCE, billing: bool = True) -> str:
    """The organization's hub role, in the management account: trusts the room's identity,
    may assume the spoke role in every member account, reads the organization, and (if
    asked) the consolidated bill. Nothing else lives in the management account."""
    tag = version().split()[0]
    who = subject or "<room subject>"
    bill = "true" if billing else "false"
    return (
        'module "kobo_access_hub" {\n'
        f'  source = "{HUB_MODULE_SOURCE}?ref={tag}"\n\n'
        f'  integration_id                 = "{integration_id}"\n'
        f'  room_service_account_unique_id = "{who}"\n'
        f'  audience                       = "{audience}"\n'
        f'  spoke_role_name                = "{ROLE_NAME}"\n'
        f"  billing_reads                  = {bill}\n"
        "}\n\n"
        'output "kobo_hub_role_arn" {\n  value = module.kobo_access_hub.role_arn\n}\n'
    )


def hub_trust_policy(subject: str, audience: str = AUDIENCE) -> str:
    return trust_policy(subject, audience)


def hub_permissions_policy(spoke_role_name: str = ROLE_NAME, billing: bool = True) -> str:
    """What the hub may do, for an owner creating it by hand: assume the spokes, read the
    organization, read the bill."""
    statements = [
        {"Sid": "AssumeSpokes", "Effect": "Allow", "Action": "sts:AssumeRole",
         "Resource": f"arn:aws:iam::*:role/{spoke_role_name}"},
        {"Sid": "ReadOrganization", "Effect": "Allow",
         "Action": ["organizations:ListAccounts", "organizations:DescribeAccount", "organizations:DescribeOrganization",
                    "organizations:ListTagsForResource"], "Resource": "*"},
    ]
    if billing:
        statements.append({"Sid": "ReadBill", "Effect": "Allow",
                           "Action": ["ce:Get*", "ce:Describe*", "ce:List*", "budgets:ViewBudget", "budgets:Describe*", "cur:Describe*"],
                           "Resource": "*"})
    return json.dumps({"Version": "2012-10-17", "Statement": statements}, indent=2)


def spoke_terraform_snippet(hub_role_arn: str, external_id: str, integration_id: str, packs: list[str]) -> str:
    """A member account's role: trusts the organization's hub (with this account's external
    id) instead of any OIDC identity, and carries the packs."""
    tag = version().split()[0]
    pack_list = ", ".join(f'"{p}"' for p in packs)
    return (
        'module "kobo_access" {\n'
        f'  source = "{MODULE_SOURCE}?ref={tag}"\n\n'
        f'  integration_id = "{integration_id}"\n'
        f'  hub_role_arn   = "{hub_role_arn}"\n'
        f'  external_id    = "{external_id}"\n'
        f"  packs          = [{pack_list}]\n"
        "}\n\n"
        'output "kobo_role_arn" {\n  value = module.kobo_access.role_arn\n}\n'
    )


def stackset_snippet(hub_role_arn: str, external_id: str, integration_id: str, packs: list[str],
                     org_root_id: str = "<organization root id>") -> str:
    """One StackSet from the management account puts the spoke role in every member
    account: the same template, the same parameters, the organization's one external id."""
    params = cloudformation_parameters("", integration_id, packs)
    params.update({"HubRoleArn": hub_role_arn, "ExternalId": external_id})
    plist = " ".join(f"ParameterKey={k},ParameterValue={v}" for k, v in params.items() if v != "")
    return (
        "# From the management account, with the template saved as kobo-access.json\n"
        f"aws cloudformation create-stack-set --stack-set-name kobo-access-{integration_id} \\\n"
        "  --template-body file://kobo-access.json --capabilities CAPABILITY_NAMED_IAM \\\n"
        "  --permission-model SERVICE_MANAGED --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \\\n"
        f"  --parameters {plist}\n"
        f"aws cloudformation create-stack-instances --stack-set-name kobo-access-{integration_id} \\\n"
        f"  --deployment-targets OrganizationalUnitIds={org_root_id} --regions us-east-1\n"
    )


def spoke_trust_policy(hub_role_arn: str, external_id: str) -> str:
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "OrganizationHub",
            "Effect": "Allow",
            "Principal": {"AWS": hub_role_arn},
            "Action": ["sts:AssumeRole", "sts:TagSession"],
            "Condition": {"StringEquals": {"sts:ExternalId": external_id}},
        }],
    }, indent=2)


def cloudformation_parameters(subject: str, integration_id: str, packs: list[str], audience: str = AUDIENCE) -> dict[str, str]:
    """The parameters the owner fills in the console, pre-answered."""
    optional = {"security-audit": "SecurityAudit", "cost": "Cost", "logs": "Logs"}
    params = {"IntegrationId": integration_id, "GatewayServiceAccountUniqueId": subject, "Audience": audience, "RoleName": ROLE_NAME}
    for pack, name in optional.items():
        params[name] = "Enabled" if pack in packs else "Disabled"
    return params
