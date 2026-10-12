# Admin panel: design (demo wave)

Date: 2026-10-12. Status: demo built on the `admin-panel-demo` branch, not deployed.

## What it is

One admin-only panel inside the room, `#/admin/<pane>/<item>`, laid out like a
desktop settings window: a list of panes on the left, one pane on the right.
Six panes: Overview, Plugins, Connections, Roles, People, Audit.

## The model

- **Plugin.** One thing the room is wired to. A *platform* plugin is the
  room's own plumbing (deploy platform, observability, secrets, switchboard,
  gateway, directory). A *vendor* plugin is one base credential from a vendor
  (a service account or an API token with read or read/write access), held
  once by the room: Slack (baseline), GitHub App, Atlassian, Datadog, and more
  from a catalog. Absent means off, as every integration in the room already
  works.
- **Connection.** An agent's lane to one vendor through the gateway, scoped
  below the base credential: read or write, never more than the credential
  holds. Every agent is a row, so "no lane" is visible before it fails a
  reply (the janus case, 2026-10-07).
- **Role.** A named set of permissions a group holds: capabilities in the
  room, admin permissions, a ceiling per vendor (none, read, write, approve),
  and the models allowed. Four built-ins (Admin, Operator, Member, Reader) and
  custom roles from the same lists. A role's connector ceiling is what bab
  enforces as a team ceiling; the pane is bab's console once bab is deployed.
- **People.** Admin groups, operators, reader groups, and the group-request
  queue.
- **Audit.** Every change from the panel, with where it landed.

## The write path

Every change is a pull request to the org config repository (plugins, roles,
people) or to the gateway's grants file (connections), the lane the bootstrap
CLI already uses. A grant may also be applied now through the gateway's admin
plane; the pane says that one is lost with the policy volume. The room reads
org changes at its next boot and the panel says so.

Consequences: sero's three coordinates become org keys (none is a secret);
the deploy platform's card has no Connect, since its token lives in the
deployment config.

## What the demo is

`ui/src/lib/admin/store.svelte.ts` is the in-memory store with the shapes
above and example data; every pane reads it and changes it through the
functions there, each of which writes its audit row. The shapes are the
contract the API will serve. `ADMIN_DEMO=1 npm run build` builds
`admin-demo.html`, the panel inside the room's shell with no API behind it.

## Not in the demo

The API routes, the pull-request writers, the gateway admin-plane client,
bab's live verdicts, and the first-run "make me admin" card.
