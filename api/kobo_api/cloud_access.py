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
    """For an owner who creates the role by hand: the trust policy alone."""
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "GatewayOIDC",
            "Effect": "Allow",
            "Principal": {"Federated": GOOGLE_ISSUER},
            "Action": ["sts:AssumeRoleWithWebIdentity", "sts:TagSession", "sts:SetSourceIdentity"],
            "Condition": {"StringEquals": {
                f"{GOOGLE_ISSUER}:sub": subject or "<gateway subject>",
                f"{GOOGLE_ISSUER}:aud": audience,
            }},
        }],
    }, indent=2)


def cloudformation_parameters(subject: str, integration_id: str, packs: list[str], audience: str = AUDIENCE) -> dict[str, str]:
    """The parameters the owner fills in the console, pre-answered."""
    optional = {"security-audit": "SecurityAudit", "cost": "Cost", "logs": "Logs"}
    params = {"IntegrationId": integration_id, "GatewayServiceAccountUniqueId": subject, "Audience": audience, "RoleName": ROLE_NAME}
    for pack, name in optional.items():
        params[name] = "Enabled" if pack in packs else "Disabled"
    return params
