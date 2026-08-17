# Slack two lanes — channels for everyone, tokens for operators (design)

Approved in brainstorm 2026-08-17. Operator rulings: the admin group is
the existing `slack_operators` list (no new config key); the Slack tab
becomes visible to every user with the channels lane open to all; the
channel list stays editable after connection and re-wires the agent; the
Slack-side ceremony (app from manifest, workspace install, xapp mint)
stays with the operators.

Why: the person assembling an agent knows which channels it belongs in,
but never holds bot or app tokens. Today the whole tab hides unless you
are an operator, and the one form demands channel and both tokens
together. The two concerns split cleanly.

## The two lanes

### Channels (every signed-in user)

- A list editor on the Slack tab: one channel ID field to start, an
  "Add another channel" button, a remove control per row.
- Each value validates against the room's existing channel ID rule
  (`SLACK_CHANNEL_RE`) with a plain per-row error.
- Save persists the list to the room's database (channels are room
  state, not bundle state, until wiring writes transports).
- Saving while the agent is already connected triggers a re-wire (see
  Transports). Saving before tokens exist just stores the list.
- The lane freezes with the rest of the page while the agent is
  destroying (the `frozen` idiom).
- At most eight channels per agent for now (each channel holds one
  Socket Mode connection and the app token allows about ten).

### Setup (slack_operators only)

- The existing ceremony, minus the channel field: create-from-manifest
  guidance, bot token (xoxb) and app token (xapp) fields, Finish setup.
- Finish setup requires at least one saved channel; if none exist the
  button explains plainly ("Add at least one channel above first." or
  the operator adds one themselves — the channels lane is open to them
  too).
- Tokens flow to the per-agent platform secret at finish, exactly as
  today. Tokens are never stored in the room's database and never
  echoed back in any response.

## States (one status line, plain copy)

| state | meaning | shown to users |
|---|---|---|
| idle | no channels, no tokens | "Not connected. Add the channels this agent should join." |
| waiting on setup | channels saved, no tokens | "Channels saved. Waiting on an operator to finish the connection." |
| connecting | wire in flight | "Connecting..." |
| connected | wired | "Connected." plus the live channel list |
| failed | wire failed | the plain reason, and the operator lane offers retry |

The agent status payload carries the state, the saved channel list, and
`slack_admin_visible` (the renamed gate for the setup lane). The old
all-or-nothing tab hiding goes away.

## Transports and re-wire

- Wiring writes one slack transport entry per saved channel into the
  bundle's `agent.yaml` (replacing the previous slack entries
  wholesale), commits, and rides the existing CI and delivery pipeline.
  Channel edits after connection do the same: commit, CI, delivery
  chips, honest not-yet-delivered window.
- The platform secret ripple (`secrets_from`, reprovision) happens only
  when tokens first land or rotate, as today.
- Removing every channel disconnects the transports (empty slack
  transport list committed) but keeps the tokens in place; the state
  returns to "waiting" with the channels lane inviting a new list.

## The one cross-repo discovery item

Multiple channels require the runtime to serve more than one slack
transport (or one transport carrying a channel list). Task 1 of the
plan resolves which shape the deployed runtime supports by reading the
runtime's transport handling; if it supports neither, the arc includes
the smallest runtime change on a branch, delivered through the same
review-and-deploy handoff the enforcement arc used. The room-side wire
format follows whatever the runtime's real contract is (never a
lookalike).

## API surface

- `PUT /api/agents/{name}/slack/channels` with `{"channels": ["C..."]}`:
  any signed-in user; validates every entry against the real rule;
  stores the list; when the agent is connected, triggers the re-wire
  commit. Rejects with a plain per-entry reason on bad input.
- The finish-setup route slims to tokens only, reads the stored
  channels, keeps its `slack_operators` gate and its existing secret
  and reprovision mechanics.
- Channel storage: a room-database migration (list of channel IDs per
  agent, ordered, deduplicated case-sensitively).

## Testing

- State machine matrix: channels first, tokens first, edit after
  connect, remove to zero, wire failure and retry.
- Gate matrix: any user saves channels; non-operators cannot reach the
  token route; the setup lane renders only for operators.
- Validation: channel rule parity with the route's real regex, per-row
  errors, dedupe.
- UI: factories for the new payload fields; component tests (jsdom rig)
  for the list editor (add another, remove, per-row error), the lane
  visibility split, and frozen behavior.
- Live acceptance: a non-operator saves channels on a scratch agent, an
  operator finishes setup, the agent answers in both channels; then a
  channel edit re-wires and the delivery chip tells the truth
  throughout.

## Non-goals

- OAuth auto-install and any Slack-side automation of the app-creation
  or xapp ceremony (roadmap, explicitly deferred).
- Per-channel behavior or permissions (one agent, one behavior,
  wherever it listens).
- Slack workspace discovery or channel pickers backed by the Slack API
  (typed IDs stay; a picker is a later nicety).
- Migrating existing connected agents' single channel automatically
  (the one live wired agent can be re-saved through the new lane by
  hand).
