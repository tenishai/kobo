"""The cloud-access release the room ships: what an account owner gets on the
Connect AWS page, and a project owner on the Connect Google Cloud page (design
2026-10-25, cloud access for integrations; kobo#390 for Google Cloud).

`tenishai/cloud-access` is private while the suite is, so the room does not
link to it: a pinned release is vendored under `vendor/cloud_access/` and the
page is rendered from those bytes. The packs file is the contract (which
read-only access each pack is); the CloudFormation template is served as is;
the Terraform snippet points at the module source so an owner whose
organisation can read the repo applies it from there.

Google Cloud has no role to create and no trust to federate: the permission
service's own service account is granted directly on the project, folder or
organization (the way Wiz does it), so `packs-gcp.json` is a list of roles per
pack and the page renders three ways to bind them to that one member: the
`gcloud` commands, Terraform `google_*_iam_member` resources, and the plain
list for a console.

Bedrock (kobo#390) rides an AWS account's role: when the Connect AWS sheet has
the Bedrock box ticked, the role's snippets gain one more permissions block
(`bedrock_permissions_policy`, `bedrock_terraform_snippet`) so a new install
can build the role the room's model gateway assumes. The trust policy is
unchanged: the role already trusts the room's identity.
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
# A role that serves Bedrock models to the room and nothing else (kobo#390 models-only
# shape): the Bedrock permissions, no read packs, and agents are granted nothing there.
MODELS_ROLE_NAME = "KoboModels-Bedrock"
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


# ---- Google Cloud (kobo#390) --------------------------------------------------

GCP_REQUIRED_PACK = "inventory"
GCP_MODELS_PACK = "models"
# What a grant target is: a project id, `folders/<number>` or `organizations/<number>`.
GCP_PROJECT_MODE = "project"
GCP_PARENT_MODE = "parent"


@lru_cache(maxsize=1)
def gcp_packs() -> list[dict[str, Any]]:
    """One entry per pack: id, purpose, required, and the roles it binds."""
    raw = json.loads((_DIR / "packs-gcp.json").read_text())
    return [{"id": p["id"], "purpose": p.get("purpose", ""), "required": bool(p.get("required")),
             "roles": list(p.get("roles") or [])} for p in raw["packs"]]


def gcp_chosen_packs(wanted: list[str] | None) -> list[str]:
    known = [p["id"] for p in gcp_packs()]
    picked = [p for p in (wanted or []) if p in known]
    return [GCP_REQUIRED_PACK] + [p for p in known if p in picked and p != GCP_REQUIRED_PACK]


def gcp_roles(packs: list[str]) -> list[str]:
    """Every role the chosen packs bind, once each, in pack order."""
    out: list[str] = []
    for p in gcp_packs():
        if p["id"] in packs:
            out.extend(r for r in p["roles"] if r not in out)
    return out


def _gcp_target(target: str, mode: str) -> tuple[str, str]:
    """`(kind, bare id)` of a grant target: a project id, or a folder's or
    organization's number. A blank target keeps a placeholder so the snippets
    still read whole before the person has typed one."""
    if mode == GCP_PARENT_MODE:
        if target.startswith("folders/"):
            return "folder", target.split("/", 1)[1]
        if target.startswith("organizations/"):
            return "organization", target.split("/", 1)[1]
        return "folder", "<folder number>"
    return "project", target or "<project id>"


def gcloud_snippet(email: str, target: str, roles: list[str], mode: str = GCP_PROJECT_MODE) -> str:
    """One `add-iam-policy-binding` per role, on the project, folder or organization."""
    kind, bare = _gcp_target(target, mode)
    member = f"serviceAccount:{email or '<identity email>'}"
    head = {
        "project": f"gcloud projects add-iam-policy-binding {bare}",
        "folder": f"gcloud resource-manager folders add-iam-policy-binding {bare}",
        "organization": f"gcloud organizations add-iam-policy-binding {bare}",
    }[kind]
    return "".join(f"{head} \\\n  --member={member} --role={role} --condition=None\n" for role in roles)


def gcp_terraform_snippet(email: str, target: str, roles: list[str], mode: str = GCP_PROJECT_MODE) -> str:
    """`google_<kind>_iam_member` per role, from one local list, so the owner's
    pipeline reviews the roles once and binds them to the one member."""
    kind, bare = _gcp_target(target, mode)
    resource = {"project": "google_project_iam_member", "folder": "google_folder_iam_member",
                "organization": "google_organization_iam_member"}[kind]
    where = {"project": f'project = "{bare}"', "folder": f'folder  = "folders/{bare}"', "organization": f'org_id  = "{bare}"'}[kind]
    role_list = "\n".join(f'    "{r}",' for r in roles)
    return (
        "locals {\n"
        f"  kobo_roles = [\n{role_list}\n  ]\n"
        "}\n\n"
        f'resource "{resource}" "kobo_access" {{\n'
        "  for_each = toset(local.kobo_roles)\n\n"
        f"  {where}\n"
        "  role    = each.value\n"
        f'  member  = "serviceAccount:{email or "<identity email>"}"\n'
        "}\n"
    )


# ---- Bedrock through an AWS account (kobo#390) ---------------------------------

BEDROCK_DEFAULT_REGION = "us-east-1"
BEDROCK_POLICY_NAME = "KoboModels-Bedrock"
# What the room's model gateway needs on the role: to read the catalogue (the
# foundation models and the inference profiles the role can see) and to call
# them, streaming or not. Invocation is allowed on every region's models and
# profiles because a cross-region inference profile calls a model in another
# region than the one named on the row.
BEDROCK_READ_ACTIONS = (
    "bedrock:ListFoundationModels", "bedrock:GetFoundationModel",
    "bedrock:ListInferenceProfiles", "bedrock:GetInferenceProfile",
)
BEDROCK_INVOKE_ACTIONS = (
    "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse", "bedrock:ConverseStream",
)
BEDROCK_INVOKE_RESOURCES = ("arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:*:*:inference-profile/*")


def bedrock_statements() -> list[dict[str, Any]]:
    return [
        {"Sid": "BedrockCatalogue", "Effect": "Allow", "Action": list(BEDROCK_READ_ACTIONS), "Resource": "*"},
        {"Sid": "BedrockInvoke", "Effect": "Allow", "Action": list(BEDROCK_INVOKE_ACTIONS),
         "Resource": list(BEDROCK_INVOKE_RESOURCES)},
    ]


def bedrock_permissions_policy() -> str:
    """For an owner who builds the role by hand, or attaches it after a
    CloudFormation stack: the Bedrock permissions alone, as one policy."""
    return json.dumps({"Version": "2012-10-17", "Statement": bedrock_statements()}, indent=2)


def bedrock_terraform_snippet(module_name: str = "kobo_access", role_ref: str | None = None) -> str:
    """One inline policy on the role the module made, appended under the
    module block. The role's name is the last segment of its ARN; `role_ref`
    names the role another way (a resource's own `.name`)."""
    policy = "\n".join("    " + line for line in bedrock_permissions_policy().splitlines())
    role = role_ref or f"basename(module.{module_name}.role_arn)"
    return (
        f'resource "aws_iam_role_policy" "kobo_bedrock" {{\n'
        f'  name   = "{BEDROCK_POLICY_NAME}"\n'
        f"  role   = {role}\n"
        f"  policy = <<-EOT\n{policy}\n  EOT\n"
        "}\n"
    )


def models_role_terraform_snippet(subject: str, audience: str = AUDIENCE) -> str:
    """A role that serves Bedrock models and nothing else: the room's trust
    policy and the Bedrock permissions, no module and no read packs, so the
    account carries nothing an agent could be granted."""
    trust = "\n".join("    " + line for line in trust_policy(subject, audience).splitlines())
    return (
        f'resource "aws_iam_role" "kobo_models" {{\n'
        f'  name               = "{MODELS_ROLE_NAME}"\n'
        f"  assume_role_policy = <<-EOT\n{trust}\n  EOT\n"
        "}\n\n"
        + bedrock_terraform_snippet(role_ref="aws_iam_role.kobo_models.name")
        + '\noutput "kobo_role_arn" {\n  value = aws_iam_role.kobo_models.arn\n}\n'
    )
