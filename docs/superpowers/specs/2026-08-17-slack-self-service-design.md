# Slack self-service — the manifest comes to you (design)

Approved in brainstorm 2026-08-17 (evening), superseding the operator
lane of the two-lane design (same date) before it met a real user.
Operator rulings: the workspace lets any member create and install apps
from a manifest; self-service REPLACES the operator lane outright; a
user-created app that the room cannot delete at destruction time is a
done-with-note, never a blocker.

Why: the two-lane design assumed only the infra group could hold
tokens. The simpler truth is that anyone can create the Slack app
themselves when the room hands them the manifest and honest
instructions — then the tokens are theirs to paste, and nobody waits
on an operator.

What survives from the two-lane arc unchanged: the channels lane (open
to all, cap eight, editable, re-wire on save), the state machine and
its copy, one-transport-per-channel wiring, the delivery chips, tokens
never stored in the room database.

## The ceremony (one lane, every signed-in user)

The setup section of the Slack tab, no gate:

1. **Get the manifest.** A "Show the manifest" control reveals the
   agent's rendered manifest (the same renderer the retired API-create
   path used) in a copyable block.
2. **Plain numbered instructions**, no jargon beyond Slack's own:
   create an app from a manifest at Slack's app site, install it to
   the workspace, create an app-level token that can connect (Basic
   Information, App-Level Tokens), copy the bot token (OAuth page) and
   the App ID (Basic Information).
3. **Three paste fields**: App ID, bot token (xoxb), app token (xapp),
   then Finish setup. Finish requires at least one saved channel, as
   today.

## API surface

- `GET /api/agents/{name}/slack/manifest`: any signed-in user; returns
  the rendered manifest text. The renderer is extracted from the
  retired create-app path and shared, never duplicated.
- The connect route loses its operator gate and gains `app_id` in the
  body, validated against Slack's real App ID shape (A-prefixed id,
  same validation discipline as the channel rule — grounded, never a
  lookalike) and stored in the existing `slack_app_id` column. Token
  mechanics (secret delivery, transports, reprovision-on-change) are
  byte-identical to today.
- The config-token API-creation route (`POST .../slack/app`) retires.
  The config-token client itself stays: destruction still uses it to
  delete legacy room-created apps.
- `slack_admin_visible` leaves the agent payload; the UI stops gating
  the setup lane. (`slack_operators` remains in the org config for the
  room's other uses; this flow no longer reads it.)

## Destruction: done with a note

`destroy-slack` behavior grows one tolerance. The step still attempts
`apps.manifest.delete` with the room's config token:

- Room-created apps (legacy, e.g. agents wired before this design):
  deleted as today.
- User-created apps: Slack refuses the delete for an app the config
  token's owner cannot manage. That refusal class (grounded in Slack's
  real error codes during implementation, not guessed) completes the
  step with the recorded note, verbatim:
  "The Slack app belongs to its creator. Remove it in Slack."
- Absent apps and absent ids keep their existing already-gone
  tolerances.

Refinement (Task 1): the room-created/user-created distinction above is
recorded directly, not inferred — the degated connect route stamps
`slack_app_self_service` at connect time from how the app was actually
born, so destroy-slack reads that flag instead of classifying Slack's
error codes after the fact.

The destruction spec's honest inventory gains the matching caveat: a
user-created Slack app survives destruction until its creator removes
it, and the destroy summary says so.

## Testing

- Manifest route: gate matrix (signed-in yes, anonymous no via the
  app-wide middleware), rendered content matches the shared renderer's
  output for the agent.
- Connect: app-id validation matrix (shape rule parity test), gate
  REMOVED (non-operator 200 path), tokens+id stored/delivered exactly
  as the current route tests prove, 409-without-channels preserved.
- Destroy-slack: refusal-class mapping test (ownership error → done
  with the exact note; other errors still fail the step), legacy
  delete path regression.
- UI: instructions render for everyone; manifest block copyable; three
  fields validate client-side with the server's exact copy; frozen
  matrix still holds.
- Live acceptance: a user (no operator hat) walks the ceremony end to
  end on a real agent; the agent answers in its channels; destroying a
  self-service agent produces the note.

## Non-goals

- OAuth auto-install (still roadmap).
- Automating the xapp mint or the workspace install (Slack owns those
  screens).
- Migrating network's legacy wiring (it keeps working; its app remains
  room-created and room-deletable).
- Slack workspace admin-approval flows (the workspace allows member
  installs today; if that changes, the instructions page grows a
  sentence, not a feature).
