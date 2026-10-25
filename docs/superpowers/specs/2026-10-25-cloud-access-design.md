# Cloud access for integrations: least privilege, set up like a vendor would

Date: 2026-10-25. Status: proposal for Kyle; nothing built past the credential
kinds (#229). Companion to `2026-10-18-one-runtime-and-admin-panel-design.md`
(Arc 2, Integrations) and the bab plan.

## The problem

An AWS, GCP or Azure integration is a role the gateway assumes, not a key
(#229). But "one org-level role with admin, assigned to kobo" is an ask no
cloud owner should grant, and we should not want it: every agent in the
room would stand behind one all-powerful identity. The model has to answer
three questions before anyone pastes an ARN:

1. **What may the role do?** A small, named set of permissions, chosen per
   integration, never admin.
2. **Where does it exist?** One account, or every account in an organisation.
3. **Who may assume it, and as whom?** The gateway, bound to its own identity,
   and each agent visibly, so the cloud's audit log names the agent.

## The model

### Packs, not policies

An integration is granted **packs**. A pack is a named, versioned set of
permissions with a one-sentence purpose, shipped by us, reviewed once by the
cloud owner, and reused by every room. The first packs, from what the dev
room's agents actually do today:

| Pack | AWS | GCP | Azure | Used by |
|---|---|---|---|---|
| `inventory` (default, read) | `ViewOnlyAccess` | `roles/viewer` | `Reader` | every agent with a lane |
| `security-audit` | `SecurityAudit` + CloudTrail `LookupEvents` | `roles/iam.securityReviewer`, `roles/logging.privateLogViewer` | `Security Reader` | ledger (audit), clio |
| `cost` | `ce:Get*`, `compute-optimizer:Get*`, `trustedadvisor:List*` | `roles/billing.viewer` | `Cost Management Reader` | keystone |
| `logs` | CloudWatch Logs `FilterLogEvents`, `GetLogEvents` on named groups | `roles/logging.viewer` | `Log Analytics Reader` | sherlock |

**A real grant to calibrate against.** sherlock's service account today holds twenty
roles on the org's "Shared Infrastructure" folder (the GCP analogue of an OU scope):
`compute.viewer`, `container.viewer`, `container.clusterViewer`, `gkehub.gatewayReader`,
`run.viewer`, `cloudfunctions.viewer`, `cloudsql.viewer`, `alloydb.viewer`, `redis.viewer`,
`pubsub.viewer`, `dns.reader`, `artifactregistry.reader`, `bigquery.metadataViewer`,
`servicehealth.viewer`, `logging.viewer`, `monitoring.viewer`, `errorreporting.viewer`,
`cloudtrace.user`, `secretmanager.viewer` (metadata only, never `secretAccessor`), and the
blanket `roles/viewer`, which the IAM recommender scores at 6199 of 6220 permissions excess.
That list is the GCP `inventory` pack (the first fourteen) and `logs` pack (the next four),
with `secretmanager.viewer` moving to `security-audit`, and `roles/viewer` dropped: the
specific viewers already cover what the agent uses, and the blanket role is what makes a
cloud owner say no. The bucket grant on `fubo-agent-memory-replicas` is kobo's own memory
replica, not a pack.

Write packs come later and separately (`act:ecs-restart`, `act:tag`), each
its own pack with its own review, never folded into a read pack. No pack ever
carries `iam:*` write, `kms:Decrypt` on everything, or data-plane reads of
customer data (the Wiz "data scanning" shape is explicitly out: agents reason
about infrastructure, they do not read S3 objects).

### One role per integration, one session per agent

- The cloud owner creates **one role per integration** (per account, or via
  a StackSet / folder binding across an organisation) carrying the chosen
  packs. It trusts exactly one principal: the gateway's Google service
  account, through the cloud's own federation (AWS `AssumeRoleWithWebIdentity`
  with `accounts.google.com` as the OIDC provider bound to the service
  account's unique id; GCP workload identity federation or impersonation;
  Azure a federated credential on an app registration). No long-lived key
  anywhere, and no external ID needed because the identity binding is
  stronger than a shared secret. External ID stays supported for the hub
  pattern (a fubo AWS hub role assumes the per-account role).
- The gateway assumes the role **once per agent lane**, with
  `sts:SourceIdentity = agent:<name>` and a **session policy** that narrows
  the role to the lane's access (`read` = the packs' read statements only;
  `write` = the role's full set). CloudTrail, Cloud Audit Logs and Azure
  Activity Log then show which agent acted, and a `read` lane can never do
  more than read even if the role could.
- kobo keeps, per integration: `scope` (account / org + OU ids, project /
  folder, subscription / management group), `packs`, `role_arn` (or service
  account / app registration), `trusted_principal` (the gateway identity it
  was told), `module_version` (what was deployed), and `verified_at` (the
  last successful test assume).

### GCP, the way Wiz does it

Wiz's own GCP set-up at the org (seen 2026-10-25) is the pattern to copy, with
narrower contents. Its service account lives in Wiz's project and is granted
directly on the customer's org: predefined roles (Browser, Cloud Asset Viewer,
Security Reviewer, Service Usage Viewer, Viewer, KMS CryptoKey
Encrypter/Decrypter) plus custom roles, one base (`wiz_security_role`) and one
`_ext` per optional capability (data scanning, disk analysis, forensic,
registry scanning, serverless scanning), the forensic one under an IAM
condition. Three things carry over:

- **The gateway's service account is granted directly.** It already lives in
  a Google project of ours, so on GCP there is no federation step at all: the
  module grants our SA on the folder or org the owner names, and
  `gcp_identity` with no service account means exactly that. Impersonating a
  narrower SA stays the optional second shape.
- **Packs are custom roles.** The module creates `kobo_inventory`,
  `kobo_logs`, `kobo_security_audit`, `kobo_cost` as org- or folder-level
  custom roles built from the permission lists in `packs/gcp.json`, so a cloud
  owner reviews four named roles, not twenty predefined ones, and an upgrade
  is a role edit. Where a predefined role is exactly a pack (`roles/viewer` is
  not; `roles/logging.viewer` is close) the module may bind it instead.
- **Conditions for the narrow cases.** A pack that must be limited to a
  resource prefix or a folder gets an IAM condition, the way Wiz limits the
  forensic role; kobo shows the condition on the integration page.

What does not carry over: `roles/viewer`, `roles/browser` across the org and
KMS decrypt. Those are a security scanner's needs, not an agent's, and they
are what makes an owner refuse.

### Set-up, the way Wiz does it

A "Connect AWS" (GCP, Azure) page, not a form:

1. **About this integration.** Three sentences: what agents get, what the
   role can never do, where the audit trail lands. A link to the packs'
   permission lists.
2. **Scope.** Organisation (recommended; the module deploys to the OUs you
   name) or one account.
3. **Packs.** Checkboxes with one line each; `inventory` on and locked.
4. **Deploy.** Three tabs, each pre-filled with this room's values (the
   trusted principal, the integration id, the packs, the module version):
   - **Terraform**: a module invocation, `source` pinned to a release of
     our module, copy button.
   - **CloudFormation / Deployment Manager / Bicep**: a quick-create link
     with parameters in the URL, or the template to upload.
   - **Manual**: the trust policy JSON and the policy ARNs to attach, for
     an owner who will not run our code.
5. **Finish.** Paste the role ARN (the module's output). kobo asks the
   gateway to assume it as a probe (`SourceIdentity = kobo-probe`, no pack
   used) and shows "Connected: assumed <arn> as <principal>" or the
   cloud's refusal verbatim. Nothing is "connected" until the probe passes.

The same page is the integration's detail view afterwards: scope, packs,
verified-at, the module version, and "Re-run the probe" and "Update packs"
(which re-renders the deploy snippets; the role changes only when the owner
re-applies them).

### Where the modules live

`tenishai/cloud-access` (ruled 2026-10-25: created, private for now, opened
with the rest of the suite later). It holds `aws/terraform/`,
`aws/cloudformation/`, `gcp/terraform/`, `azure/` (shape only, module later),
with the pack definitions as data files the modules, the template and kobo
all read (`packs/aws.json` ...), released as tagged versions. Because the
repo is private, kobo does not link to it: kobo vendors a pinned release
into its own tree (`api/kobo_api/cloud_access/<version>/`) and serves the
module source, the CloudFormation template and the pack text from inside
the room, so an owner on the Connect page never needs read access to the
repo. When the repo goes public the same page can add "source on GitHub"
links; nothing else changes. An upgrade is a kobo release note plus the
owner re-applying.

The role is `KoboAccess-Role` by default (ruled 2026-10-25: the product
name, the way Wiz names `WizAccess-Role`), so a cloud owner recognises one
role across every integration they grant to kobo.

### What this needs from the rest of the suite

- **liminal** (tenishai/liminal#202, extended): `aws_role` assumption via
  web identity from its own GSA, with `SourceIdentity` and a session policy
  per lane; `gcp_identity` impersonation; `azure_federated` client
  credentials. The probe endpoint the Finish step calls.
- **kobo**: the Connect page, the integration record fields above, the
  snippet renderer, the probe call; the lane grant passes the agent name
  and access so the gateway can build the session.
- **bab**: nothing new; the lane stays the unit bab reasons about.

## What I need from Kyle before building

1. **The first packs.** Is the table above right for what the agents do
   today? Anything to add for sherlock, ledger, keystone, clio before the
   first module ships?
2. **The trusted principal.** The gateway's own service account through
   OIDC (my recommendation: no shared secret, per-agent audit), or a fubo
   AWS hub role with an external ID (what Wiz does, fits an org that already
   has a hub)?
3. ~~**The repo.**~~ Ruled: `tenishai/cloud-access`, private until the
   suite goes public; kobo vendors a pinned release.

## Order of work, once ruled

1. ~~`tenishai/cloud-access`~~ done 2026-10-25 (1b047f1): packs for the
   three clouds, AWS Terraform module + CloudFormation template, GCP grant
   module, Azure shape, validate CI. First release tag once liminal can
   assume the role.
2. liminal: `aws_role` via web identity with `SourceIdentity` + session
   policy; the probe.
3. kobo: the Connect AWS page over the record fields above; GCP and Azure
   follow the same page with their own module.
