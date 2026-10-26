# Cloud access, layer two: what each agent may do inside the account ceiling

Status: proposal, 2026-10-25. Follows `2026-10-25-cloud-access-design.md`
(the ceiling: one role per account, read-only packs, built and live on dev).
Kyle's observation that prompted it: for Atlassian or Datadog a lane is the
vendor's one tool list, read or write; for AWS, Google Cloud and Azure an
agent's need is case by case across hundreds of services, so a lane that
hands over the whole account role is the wrong grain.

## The model in three sentences

The account owner approves a **ceiling** once: one role per account, built
from packs, trusting the gateway's identity (built). The room admin gives
each agent a **profile** inside that ceiling, case by case: which accounts,
which services and actions, read or a named write, optionally where. The
gateway enforces the profile on every call and the cloud's own audit shows
the agent's name and the narrowed session.

Two things this keeps that the alternatives lose. The owner is asked once
per account, not once per agent. And the profile is the room's decision, so
an agent asking for more is a request to a room admin, not a ticket to a
cloud team.

## The profile

A profile is one lane's scope. Stored on the lane (`AdminGrant.scope`,
JSON) and projected to the gateway with the grant.

```json
{
  "accounts": ["aws"],                       // integration ids; [] = every connected account the ceiling allows
  "services": {
    "ec2": ["Describe*"],                    // action globs, AWS spelling; "*" = every read the ceiling allows
    "cloudtrail": ["LookupEvents", "Get*", "List*"],
    "logs": ["*"]
  },
  "regions": ["us-east-1", "us-west-2"],     // optional; [] = any
  "resource_tags": {"team": "video"},        // optional, later: only resources carrying these tags
  "writes": ["ecs:UpdateService"]            // optional; each must be inside a write pack the ceiling carries
}
```

Google Cloud and Azure use the same shape with their own spellings
(`compute.instances.list`, `Microsoft.Compute/virtualMachines/read`); the
`services` map is "API surface → operations" on every cloud.

**Templates**, so admins never write IAM. Shipped in `cloud-access`
(`profiles/<cloud>.json`, vendored like the packs), chosen by name on the
lane and editable afterwards:

| template | services | for |
|---|---|---|
| `investigator` | ec2, elbv2, ecs, eks, rds, lambda, iam (Get/List), cloudtrail, logs, cloudwatch | sherlock-style incident work |
| `cost` | ce, budgets, cur, compute-optimizer, trustedadvisor | ledger-style spend questions |
| `security` | iam, config, guardduty, securityhub, cloudtrail, kms (describe only) | audit and posture |
| `inventory` | every service, `Describe*`/`List*`/`Get*` only | a broad read, the ceiling itself |
| custom | whatever the admin picks | one agent's odd need |

A template is a starting point; the lane stores the resolved profile, so a
later template change does not silently widen an agent.

## Enforcement, twice

1. **At the cloud, where the cloud can.** On AWS the profile becomes the
   session policy on `AssumeRoleWithWebIdentity` (already per agent, already
   tagged with `SourceIdentity`). IAM intersects it with the role, so even a
   handler bug cannot exceed the profile, and CloudTrail shows the narrowed
   session. The policy is rendered from `services`, `regions`
   (`aws:RequestedRegion`) and `resource_tags` (`aws:ResourceTag/<k>`),
   capped at IAM's packed 2048-character limit; a profile that does not fit
   is refused at save time with the size. Google Cloud and Azure have no
   general downscoping (Google's Credential Access Boundaries cover Cloud
   Storage only), so there the cloud sees the ceiling.
2. **In the gateway, on every cloud.** The handler checks the call against
   the profile before it goes out: account allowed, service and action match
   a glob, region allowed, write named. This is the only enforcement on
   Google Cloud and Azure and the second wall on AWS. The read-only verb wall
   and the data-plane deny list from layer one stay underneath everything.

Denials are one sentence naming the profile ("your lane to aws allows ec2
and cloudtrail reads; s3 is not in it, ask a room admin") and are recorded
through `ctx.audit`, so the Audit page shows what an agent tried and was
refused, which is the signal for widening a profile.

## Writes

A session policy cannot exceed the role. So a write an agent needs has to
be in the ceiling: the owner adds a **write pack** once (bounded, named,
tag-conditioned, for example `ecs-restart`: `ecs:UpdateService` on services
tagged `kobo:writable=true`), and the room hands it to one agent's profile
as a named write. Layer one's rule stands: never `iam:*` writes, never
blanket KMS, never data-plane reads. Write packs are the second release of
`cloud-access`; the first live write is a separate ruling.

## The other lane: an agent's own identity

Some agents are bespoke and deep (sherlock has twenty roles on a folder).
For them the shared ceiling is the wrong shape and the owner would rather
grant in their own IAM. kobo already creates a Google service account per
agent, so on Google Cloud the owner grants roles to **that** account and
the gateway impersonates it (`gcp_identity` with `service_account` set, the
kind kobo already records); the profile and the read-only wall still apply,
the audit still names the agent. AWS has the same shape through role
chaining: an agent role trusting the gateway role, created by the owner,
named on the lane. Both are the same lane UI with "use the agent's own
identity" picked instead of the shared account. This is how an existing
bespoke agent moves onto the gateway without losing access it has today.

## What the admin sees

Connections → agent → AWS lane opens a sheet instead of a read/write
toggle: **Accounts** (checkboxes over connected accounts), **Profile** (a
template picker that fills a service list the admin can prune or extend,
with the action globs shown plainly), **Where** (regions; tags later),
**Writes** (only the packs the ceiling carries, each a checkbox), and a
footer that says what the gateway will enforce in one sentence. Save
applies at once through the gateway's admin plane (as lanes do today) and
opens the grants pull request. Vendor lanes keep the toggle; the sheet is
for `aws_role`, `gcp_identity`, `azure_federated` integrations only.

An agent that hits a denial can ask: the existing access-request flow gains
a kind "widen a cloud profile" carrying the denied call, decided by a room
admin from the Audit page in one click ("add s3 reads to this lane").

## What it needs from the suite

- **liminal**: a grant carries per-persona extras. Today `[tool_extras]` is
  one table for every caller and the token's extras are ignored in sovereign
  mode (correctly). The policy store gains `extras: dict` per grant, written
  by `POST /admin/policy/grant`, merged over the operator table into
  `ctx.extra` for that persona only. The `aws` handler reads
  `ctx.extra["aws.profile"]`, renders the session policy, and checks every
  call. One liminal PR; the same seam serves memory and files sandboxes
  later.
- **kobo**: `AdminGrant.scope`; `admin_connections` projects it into the
  gateway grant and into `grants.toml`; the lane sheet; templates vendored
  from `cloud-access`; the access-request kind; Audit shows cloud denials.
- **cloud-access**: `profiles/aws.json` (templates), a `session_policy`
  renderer the handler and the sheet share, and write packs as 0.2.0.
- **bab**: nothing; the lane stays the unit.

## Order of work

1. liminal: per-grant extras (small, isolated) + the handler's profile
   check and session policy. Tests on the two walls.
2. cloud-access 0.2.0: templates + session policy renderer; write packs
   drafted but not shipped until ruled.
3. kobo: `scope` on the lane, projection, the sheet for cloud lanes, audit
   rows for denials. Dev roll; the first profile on a real lane.
4. The agent's-own-identity arm (Google impersonation first, since the
   per-agent accounts already exist), then the access-request kind.

## What I need from Kyle

1. **Default profile for a new cloud lane.** My recommendation: none; the
   sheet requires a template. The alternative, `inventory` by default, is
   today's behaviour and the grain you objected to.
2. **Writes.** Agree that the first write pack is a separate ruling after
   layer two is live, and that it starts with one bounded, tag-conditioned
   action?
3. **Bespoke agents.** Move sherlock onto the gateway through the
   own-identity arm when it ships, or leave it bespoke? Either is fine; the
   answer sets whether step 4 is next or later.
