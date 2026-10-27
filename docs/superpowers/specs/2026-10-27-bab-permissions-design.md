# Permissions through bab: pick at birth, review in bulk, lease for an hour

Status: proposal, 2026-10-27. Supersedes `2026-10-25-cloud-agent-profiles-design.md`
(closed unmerged) and changes the second half of `2026-10-25-cloud-access-design.md`:
the account ceiling built from packs stays; the gateway as the thing that holds
and enforces an agent's permissions goes. The contract on the other side is
bab's `docs/design-leases.md`. This document is kobo's half: what the room
shows, stores and asks bab for.

## The decision in one paragraph

An agent never holds a standing credential. When it is born, its owner picks
the permissions it needs from each connected integration, in a tree shaped
like a vendor's own permissions page. The request goes to a review queue where
admins or the security team approve or deny in bulk. An approved permission is
a grant in bab with an expiry. When a task needs an integration, the agent asks
bab, bab verifies the agent by its own pod identity, checks every permission at
that instant, and mints a vendor credential that lives for an hour. Then it
expires. kobo is the console for all of this and decides none of it.

## What a person sees

### The Permissions step at creation

A new step in the create dialog, after Identity and before Review. One
expandable section per connected integration, like the screenshot of a
monitoring vendor's role editor that prompted this:

```
Permissions                                  12 enabled (9 read, 3 write)   Search...   Show templates

v Datadog                                                     4 read, 1 write
    Monitors
      [x] Read monitors          List and read monitors                              read
      [ ] Write monitors         Create, edit and mute monitors                      write
    Logs
      [x] Read logs              Search and read log events                          read
  ...
v AWS (prod account)                                          5 read, 2 write
    Compute
      [x] Describe EC2           Describe instances, volumes, images                 read
      [x] Update ECS services    Change a service's task definition or count          write
    Billing
      [ ] Read cost and usage    Cost Explorer, budgets, reports                      read
> GitHub                                                      nothing picked
```

- The tree is bab's catalogue for that integration (`GET /catalogue/{id}`),
  grouped by the vendor's own grouping. The tier is a column, never a
  checkbox pair.
- The header counts what is enabled by tier. A permission above the
  integration's ceiling is shown greyed with the reason ("the connection is
  read-only"), not hidden, so an owner knows what to ask the admin for.
- **Show templates** offers the packs and profiles already shipped in
  `cloud-access` (investigator, cost, security, inventory) as a starting
  pick; the saved request is the resolved list, never the template name.
- Nothing picked is a valid choice. The agent is born with no access and the
  step can be revisited from the agent page.

Submitting the dialog creates the agent and posts one request per
integration to bab (`POST /requests`). The agent's checklist gets a row,
"Permissions requested, waiting for review", that turns green when decided.

### Review, under Admin

A new pane, **Review**, in the admin panel's settings-screen shape. One row
per pending request: agent, owner, integration, the permissions by tier
("4 read, 1 write"), when asked. Expand a row to see the list. Checkboxes on
rows, a toolbar with **Approve** and **Deny**, a reason box the deny path
requires. Bulk decide posts `POST /requests/decide` with every selected id.

Who may decide is a bab tuple, not a kobo setting: the room's admin team or
the security team; the destroy tier needs the security team. The pane shows
what the signed-in person may decide and greys the rest. The Overview page
gets a line, "3 permission requests waiting", linking here.

### The agent page: Access

The lanes chip list on the agent page becomes an **Access** section: one
row per granted permission with its tier and **expires** column
("standing", "in 12 days", "expired"). Actions: **Request more** (reopens
the Permissions step for this agent), **Re-request** on an expiring row,
**Revoke now** for admins (with "also end open sessions" for clouds that
support it). Data from `GET /agents/{agent}/grants`.

### Activity

The Activity view (shipped against the gateway's audit this week) changes
its source to bab's leases: one row per lease, with the integration, the
permissions it covered, the task note, issued and expired times, and a link
into the vendor's own log for that window where one exists (CloudTrail by
`SourceIdentity`, GitHub audit by installation). No per-call rows from a
proxy, because there is no proxy.

## What kobo stores

Less than today. kobo keeps the UI state and nothing bab owns:

- `admin_integrations` stays: the connection, its credential reference, its
  ceiling facts (the packs the role carries). On connect, kobo tells bab the
  ceiling (`room ceiling_p integration:<id>` for every permission the
  connection can honour).
- `admin_grants` (lanes) is retired once bab is live. Requests, decisions,
  grants and leases live in bab's ledger; kobo reads them through bab's API
  and caches nothing durable.
- The create dialog's picked permissions are not stored by kobo at all; they
  go to bab as the request.

## What kobo asks bab

| kobo does | bab route |
|---|---|
| render the Permissions step | `GET /catalogue/{integration}` |
| submit the create dialog | `POST /requests` (one per integration) |
| show Review | `GET /requests?status=pending` |
| bulk approve or deny | `POST /requests/decide` |
| show Access on the agent page | `GET /agents/{agent}/grants` |
| Revoke now | `DELETE /grants/{id}` |
| show Activity | `GET /leases?agent=&since=` |
| connect an integration | writes the ceiling tuples |

kobo calls bab with its own service identity and passes the signed-in
person as the requester or decider. bab records both. kobo never holds a
vendor credential for an agent and never sees a lease.

## The runtime side, for the contract

The agent runtime gets a credential provider: when a tool first touches an
integration during a task it asks bab for a lease with its projected service
account token, caches the credential until a minute before expiry, and asks
again after. A refused permission returns to the model as the tool result,
naming the permission and saying to ask the owner. A pending gated request
returns "waiting for approval". When bab or its store is unreachable, no new
lease is minted; leases already held keep working until they expire. This
is bab's and the runtime's to build; kobo only shows the outcome.

## Base install, then bab

The rule from the gateway work holds: kobo never depends on a sibling. bab
is an add-on, shown as **Access** on the Services page, off until the org
config names `bab_url`. With it off, the Permissions step, Review pane and
Access section are hidden and the room runs as it does today. With it on,
lanes are hidden and the gateway's grant projection stops. An install may
run both during the move; the Services page says which one is live.

## What this replaces, concretely

- The profile layer (`#232`): the approved permission set is the profile,
  and bab builds the session policy from it at lease time. `cloud-access`
  keeps the packs and profile templates as the picker's templates.
- The gateway's `registry.toml` and `grants.toml` as the permission
  authority, and kobo's pull requests against the gateway repo for lanes.
  The gateway, where it stays for MCP vendor tools, asks bab at dispatch.
- The Connect AWS sheet changes one word: the role trusts bab's identity,
  not the gateway's.
- Checkbox lanes per vendor on the Connections pane: gone. Connections shows
  what each agent holds, by tier, with expiries.

## Order of work, kobo's half

Each step ships behind `bab_url` and is a no-op without it.

1. **Access service row and bab client**: the Services page row, `bab_url`
   in the org config, a thin client with the routes above, health in the
   room's checks.
2. **Permissions step**: the tree from the catalogue, counts, templates,
   ceiling greying; requests posted at create; checklist row.
3. **Review pane**: pending list, bulk decide, reason, Overview count.
4. **Access on the agent page**: grants with expiry, re-request, revoke.
5. **Activity from leases**; lanes hidden when bab is on; gateway
   projection stops; connect writes ceilings.
6. **Guide**: the Getting started section on access rewritten in the same
   pull request as each step above.

bab's increments two to five (model, service, deploy, runtime) come first;
step 1 can start as soon as bab answers `GET /catalogue`.

## Defaults we will take unless told otherwise

- Lease length one hour; thirty minutes for the destroy tier.
- Grant validity: standing for read, 30 days for write, 7 days for destroy,
  then re-request.
- Reviewers: the room's admin team or the security team; destroy needs
  security.
- A request with nothing picked is allowed and creates no bab request.
