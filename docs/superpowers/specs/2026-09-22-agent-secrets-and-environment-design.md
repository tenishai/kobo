# Secrets and environment, per agent -- design

Status: drafted 2026-09-22 from the operator's ask ("do we have a section where they can add
in their secrets, like the deploy platform's app settings do?"). Built under the standing
delegation; the operator may amend any section before it ships.

## Why

The room creates two secrets for every agent it builds (the colleague pass and the Slack
tokens) and, since the secrets platform integration landed, writes them through the
platform's machine lane. Nothing lets an owner add a secret of their own: an API key the
agent needs, a database URL, a webhook signing secret. Today they would have to make a
Kubernetes Secret by hand in the agent's namespace and edit `gojira.yaml` to mount it, which
is exactly the kind of infrastructure vocabulary the room exists to remove. The connect form
even had a "Secret names" field for this and the operator had it removed, because naming a
secret you must then create elsewhere is not a feature.

The sibling deploy platform shows the shape people already understand: one settings section,
"Secrets" for anything sensitive and "Environment variables" for the rest, keys visible,
values write-only, mounted into the running service without a redeploy.

## What it is

A **Secrets and environment** panel on every agent page, composed and connected alike, for
anyone who can edit the agent (owner, group members with write, operators).

The gate is owner-or-operator exactly as every other write surface has it, and deliberately
NOT widened by a per-user grant the way the learnings decide gate is. A grantee may talk to an
agent and teach it, which is what that widening was for; reading which credentials it holds and
replacing them is a different thing, and the narrower gate is the one to start from because it
can be widened later and cannot be narrowed without taking something away.

Two lists, as the sibling has them:

| | Secrets | Environment variables |
|---|---|---|
| Held where | the secrets platform, namespace `kobo`, path `apps/<agent>/env` | the agent's `gojira.yaml`, under `env` |
| Reaches the pod as | environment variables, from Kubernetes Secret `<agent>-env` | environment variables, from the deploy platform's rendered Deployment |
| Shown in the room | key names only, never a value, not even masked | key and value |
| Changed how | written through the machine lane, synced by the External Secrets Operator within a minute | a commit (composed) or a pull request (connected), same rules as wiring |
| Takes effect | on the next start of the pod | on the next deploy, which the change triggers |

The Kubernetes Secret keeps the name pattern the platform already uses (`<agent>-cabal`,
`<agent>-slack`, now `<agent>-env`), and the room appends `<agent>-env` to `secrets_from` in
`gojira.yaml` once, the first time a secret is added. The deploy platform turns `secrets_from`
into `envFrom` today for the room's two existing secrets, so nothing new is asked of it.

### The panel

- **Secrets.** A table of key names with the date each was last set. "Add secret" opens a
  key and a value field; the value is sent once and never shown again. "Remove" takes a key
  away. "Paste .env" takes `KEY=VALUE` lines and adds them all, the way the sibling does; lines
  that are not `KEY=VALUE` are shown back and nothing is written until they are fixed.
- **Environment variables.** Key and value, editable inline, the same three actions.
- One sentence above the lists, plain: "Secrets are stored in the secrets platform and reach
  the agent as environment variables. Environment variables are for values that are not
  sensitive. Changes reach the agent the next time it starts." A "Restart agent" button sits
  beside it when there are unsaved-to-pod changes, using the existing redeploy action, so
  nobody has to know what a rollout is.
- Key rules, enforced server-side and explained inline: `[A-Z][A-Z0-9_]*`, at most 64
  characters, at most 100 keys per list. Names the room itself manages (`CABAL_COLLEAGUE_TOKEN`,
  `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET`, and every key the model env
  sets) are refused with the sentence "the room sets this one".
- When the room has no secrets platform configured, the Secrets list is replaced by one
  sentence saying so; environment variables still work.

### The API

Under the agent, same guards as every other write route (the connected-agent guard included,
since the panel exists for connected agents too):

- `GET /api/agents/{name}/secrets` -> `{"keys": [{"name", "set_at"}], "synced": bool}`.
  Key names come from the machine lane's new read route; `synced` says whether the
  ExternalSecret reports the Secret as current.
- `PUT /api/agents/{name}/secrets` body `{"set": {"KEY": "value"}, "remove": ["KEY"]}` ->
  the same shape as GET. One call writes through the lane (`values` + `remove_keys`), ensures
  the ExternalSecret exists (`sync`), and on the first ever secret appends `<agent>-env` to
  `secrets_from` and commits (composed) or opens the wiring pull request (connected). The
  response says which of those happened. Values never appear in logs, audit rows or errors.
  Removing the LAST key is allowed and is the mirror of that first write: the path and the
  ExternalSecret are deleted and the `secrets_from` entry comes back out, because a pod whose
  `envFrom` names a Secret that no longer exists does not start. The wiring change rides its
  own branch (`kobo/secrets-wiring`), separate from the environment one (`kobo/env`), so a
  pending pull request for one is never mistaken for the other.
- `GET` and `PUT /api/agents/{name}/env` for the plain variables, `PUT` with the same body
  shape; writes `env` in `gojira.yaml` by commit or pull request and triggers a deploy.
- MCP parity, because every room surface has it: `kobo_list_agent_secrets` (names only),
  `kobo_set_agent_secrets`, `kobo_set_agent_env`. Values pass through the tool call once and
  are bounded like every other tool argument.

### Destroy

Destroy gains one step, after the platform app is removed: delete `apps/<agent>/env`,
`apps/<agent>/cabal` and `apps/<agent>/slack` through the lane's new delete route, and the
three ExternalSecrets through its sync delete. This closes the gap the secrets platform
integration documented ("secrets written through the platform are not removed when the agent
is destroyed"). Idempotent, so a retried destroy does not fail on an already-deleted path.

### What the secrets platform needs first

The machine lane today has only `POST /api/service/secrets` and `POST /api/service/sync`.
This design needs, and a sibling change to the platform adds: `GET /api/service/secrets`
(key names and version, never values), `DELETE /api/service/secrets` (whole path, dry-run by
default), and `DELETE /api/service/sync` (the ExternalSecret the platform manages). Same
caller verification, same namespace pinning, same audit rows.

## What this does not do

- No reveal. A value written here is never readable in the room, and the secrets platform's
  own reveal stays behind its gate. Someone who needs to see a value goes to the platform UI
  with their own identity.
- No per-agent namespaces in the secrets platform. All agents' secrets live under `kobo`,
  because the room writes them on the owner's behalf and no person edits them by hand.
- No file mounts, no secret-as-volume. Environment variables only, as the sibling does.
- No history or rollback of values; the platform keeps KV versions if that is ever needed.

## Build order

1. Secrets platform lane: read and delete routes (separate repository; in flight).
2. Room API: the four routes and the destroy step, against the room's existing lane client,
   with a fake lane in tests. `secrets_from` and `env` edits reuse the wiring code that
   already writes `gojira.yaml`.
3. The panel, with tests for: names only, paste .env parsing and refusal, reserved-key
   refusal, the no-platform sentence, the connected-agent pull-request message.
4. MCP tools, last.
