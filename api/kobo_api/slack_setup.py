"""Slack connect (P2c): app-manifest rendering, plus the wire flow that
delivers pasted Slack tokens to the agent's namespace and threads the
channel/secrets contract through its repo.

Two independent responsibilities live here:

- `render_manifest` builds the Slack app manifest kobo hands to
  `SlackClient.create_app` (kobo_api.clients, Task 2). Its scopes/events
  are a deliberate SUPERSET of the runtime kit's canonical template
  (`docs/slack-app-manifest.yaml` in the daedalus repo) — public-channel
  grants plus private-channel grants (`groups:history`/`message.groups`)
  together, not a straight copy of one or the other. See the P2c incident-1
  writeup (`docs/superpowers/plans/p0-e2e-runbook.md`, "P2c results — slack
  connect live") for why: a live connect crashed on a private channel
  because the manifest only carried public-channel grants, fixed by
  rendering both grant sets so kobo never has to guess a channel's type
  up front.

- `wire_slack` mirrors `kobo_api.steps.WireStep`'s pattern verbatim (clone
  -> edit YAMLs -> commit_push -> reprovision-if-changed -> wait-active):
  it delivers the bot/app tokens to the cluster as a namespace Secret, edits
  `bundle/agent.yaml`'s transports list (one entry per channel, via
  `_update_agent_yaml_transports`) and `gojira.yaml`'s `secrets_from`
  list in a fresh clone, pushes, and — the plan's self-review CORRECTION —
  reprovisions gojira when that push actually changed something, since
  `secrets_from` is a generator field the deploy platform reads to build the
  Deployment's envFrom. It always confirms gojira reaches `active` before
  returning, even on the no-change path, for the exact crash-window reason
  `WireStep` documents: a prior attempt may have crashed after calling
  `reprovision()` but before its own wait finished.

  `rewire_slack_channels` is the commit-only sibling for after an agent is
  already connected: it changes only which channels the slack transport
  lists, reusing the same clone/edit/commit/push path minus every bit of
  `wire_slack`'s secret and reprovision machinery.
"""

from __future__ import annotations

import logging
import re
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import yaml

from kobo_api import delivery
from kobo_api.clients import ClientError, GitHubClient, GojiraClient, KubectlClient
from kobo_api.composer import (
    SLACK_TOOLS_ALLOW,
    repin_commit_note,
    update_agent_yaml_allow,
    update_agent_yaml_slack_read,
    update_dockerfile_runtime_image,
)
from kobo_api.repo_lock import repo_write_lock
from kobo_api.sero import SeroClient, deliver_secret

log = logging.getLogger(__name__)

# --- render_manifest -----------------------------------------------------------

# Slack's own display_information.description limit.
_DESCRIPTION_MAX_LEN = 100

# Not agent-specific; copied verbatim from the canonical template.
_BACKGROUND_COLOR = "#3d2f5b"

# Bot scopes/events: a DELIBERATE SUPERSET of the runtime kit's canonical
# manifest (daedalus/docs/slack-app-manifest.yaml) — public-channel grants
# (channels:history/message.channels) plus private-channel grants
# (groups:history/message.groups), both included together. Not a
# byte-identical copy: P2c incident 1 (docs/superpowers/plans/
# p0-e2e-runbook.md, "P2c results — slack connect live") was a live connect
# crashing on a private channel because the manifest carried only the
# public-channel half of this list — rendering both grant sets means kobo
# never has to know a channel's type before connecting it.
_BOT_SCOPES = [
    "app_mentions:read",
    "channels:history",
    # Private channels (2026-08-27 operator feedback): history alone reads
    # messages, but resolving the channel itself (conversations.info on a
    # G-prefixed id) needs the matching :read scope -- without it an agent
    # invited into a private channel can hear but not orient.
    "groups:history",
    "groups:read",
    "channels:read",
    # Direct and group DMs, the other half of the same feedback: people
    # expect to DM an agent, not only @-mention it in channels.
    "im:history",
    "im:read",
    "im:write",
    "mpim:history",
    "mpim:read",
    "chat:write",
    # The agent's face on every message (2026-11-03): the runtime posts with the
    # room-served picture as the per-message icon (SLACK_ICON_URL), which Slack
    # allows only under this scope. A SCOPE addition: an existing agent's app
    # takes it on its next reinstall (Reconnect the Slack app); until then the
    # runtime's first post with an icon is refused once and it posts plain.
    "chat:write.customize",
    # Vision (wave 3): downloading a message's image attachments via
    # files.url_private requires this read scope. A SCOPE addition --
    # unlike the tagged-only event change -- requires each existing
    # agent's Slack app to be reinstalled to its workspace after the
    # manifest re-apply; new agents get it from this generator.
    "files:read",
    "reactions:read",
    "reactions:write",
    # Who someone is (2026-11-04): a thread read names an author or a
    # reactor only by Slack id without users:read, so an agent auditing an
    # approval could not say whose :+1: it was; users:read.email lets a
    # search find a person by email when the message names them by id. A
    # SCOPE addition: an existing agent's app takes it on its next reinstall.
    "users:read",
    "users:read.email",
    # What teams asked an agent to do next in Slack (issue #484, 2026-11-13):
    # hand back a file (a report, a chart, a log), know the workspace's own
    # emoji by name, and say who is in a user group someone mentioned. Three
    # SCOPE additions in one batch, so an existing app is asked once: a
    # room-made app shows Update in Slack on its row (`scopes_owed`), one
    # from the Slack tab takes them at its next Reconnect the Slack app.
    "files:write",
    "emoji:read",
    "usergroups:read",
    # What an agent does in Slack besides answer (issue #484, 2026-11-14): its
    # own slash command (`commands`), a post in a public channel nobody
    # invited it to (`chat:write.public`), and a channel's pins and bookmarks.
    # Six SCOPE additions in one batch, for the reason above: an installed
    # app is asked once, with Update in Slack.
    "commands",
    "chat:write.public",
    "pins:read",
    "pins:write",
    "bookmarks:read",
    "bookmarks:write",
]

# Tagged-only in channels (2026-08-31 operator ruling, from live use: an
# agent in a busy thread answered EVERY reply): no message.channels or
# message.groups subscription at all -- in channels, public or private,
# only an explicit @-mention reaches the agent. DMs and group DMs keep
# full message delivery; messaging an agent directly IS the ask. The
# history/read SCOPES above stay: delivery and permission are different
# axes, and an agent asked to orient can still look a thread up. One
# behavioral consequence to say out loud: thread context now accumulates
# only from mentioned messages, so the mention should carry the ask.
_BOT_EVENTS = [
    "app_mention",
    "message.im",
    "message.mpim",
    # Reactions (2026-11-13): teams build on emoji (a tick to approve, an
    # eyes to claim), and an agent that brings its own runtime already
    # listens for them. Delivery only: reactions:read has been a scope
    # since the first manifest, so this is an EVENT addition and an existing
    # app takes it the next time its manifest is applied, with no reinstall.
    # The room's own runtime drops every event that is not a message or a
    # mention, so nothing changes for an agent until its runtime reads them.
    "reaction_added",
    "reaction_removed",
    # Someone joined a channel the agent is in, the agent itself included
    # (issue #484): how an agent learns it was just invited somewhere. An
    # EVENT addition under channels:read / groups:read, which every manifest
    # has carried; the room's own runtime drops it like the reactions above.
    "member_joined_channel",
]

# How Slack wakes an agent in a channel (2026-10-06). The runtime (daedalus
# transports/slack.py) enforces the mode from SLACK_WAKE_MODE and tells the
# model which messages reach it; the manifest has to deliver them in the
# first place, which is the one thing only the Slack app can decide.
WAKE_MENTION = "mention"  # only an @-mention; the 2026-08-31 rule and the default
WAKE_THREAD = "thread"  # a mention, then every reply in a thread it was asked in at the top
WAKE_CHANNEL = "channel"  # every message in the channel
WAKE_MODES = (WAKE_MENTION, WAKE_THREAD, WAKE_CHANNEL)
# New agents are born following their threads: that is what people expect
# of a Slack bot, and what the first user to ask was told (wrongly) it did.
WAKE_DEFAULT_NEW = WAKE_THREAD
# The env var the runtime reads; the room writes it at birth and on change.
WAKE_ENV = "SLACK_WAKE_MODE"
# Delivery of unmentioned channel messages, which `thread` and `channel`
# need and `mention` must not have (or the app hears every reply again).
_CHANNEL_MESSAGE_EVENTS = ["message.channels", "message.groups"]


# A reaction can wake the agent too (2026-11-13): the emoji an owner names,
# beside the mode and whatever the mode is. The runtime reads them from this
# variable, comma-separated; no value means a reaction never wakes the agent.
WAKE_EMOJI_ENV = "SLACK_WAKE_EMOJI"
WAKE_EMOJI_MAX = 10
_EMOJI_NAME = re.compile(r"^[a-z0-9_+\-]{1,64}$")
# Slack names the two thumbs `+1` and `-1` in its events; people type these.
_EMOJI_ALIASES = {"thumbsup": "+1", "thumbsdown": "-1"}
# The marks every agent leaves on a message it answers. As a wake emoji they
# would wake this agent each time any other agent answered anything nearby.
_STATUS_EMOJI = ("eyes", "white_check_mark", "x", "octagonal_sign")


# Where a slash command is answered (2026-11-14). The app's token can post in
# any public channel, so the runtime answers a command only in a conversation
# the agent is in unless this variable says `on`. No value is off.
COMMAND_PUBLIC_ENV = "SLACK_COMMAND_PUBLIC"
COMMAND_PUBLIC_ON = "on"


def command_public(agent) -> bool:
    """Whether the agent answers its slash command in a public channel it
    was never added to."""
    return bool(getattr(agent, "slack_command_public", False))


# What another Slack app's @-mention of the agent may do (2026-11-17). The
# runtime answers one with knowledge-only tools unless this variable says
# otherwise: the words came from a program, and some programs post text an
# outsider wrote (an alert body, an issue title). `full` gives that mention
# the tools a person's gets, `off` drops it. No value is `knowledge`, what
# every agent did before the choice. Another app's plain message never wakes
# an agent, whatever this says, and the runtime's own bounds on app-woken
# turns (three in a row in a thread, thirty an hour from one app) hold in
# every mode.
BOT_MENTIONS_ENV = "SLACK_BOT_MENTIONS"
BOT_MENTIONS_KNOWLEDGE = "knowledge"
BOT_MENTIONS_FULL = "full"
BOT_MENTIONS_OFF = "off"
BOT_MENTION_MODES = (BOT_MENTIONS_KNOWLEDGE, BOT_MENTIONS_FULL, BOT_MENTIONS_OFF)


def bot_mentions(agent) -> str:
    """What another app's mention may do, with NULL (every row from before
    the choice) read as `knowledge`."""
    mode = getattr(agent, "slack_bot_mentions", None)
    return mode if mode in BOT_MENTION_MODES else BOT_MENTIONS_KNOWLEDGE


def reads_channels(agent) -> bool:
    """Whether the agent may read the channels its Slack app is in."""
    return bool(getattr(agent, "slack_read", False))


def wake_emoji(agent) -> list[str]:
    """The emoji whose reaction wakes the agent, in the order they were given."""
    return [name for name in (getattr(agent, "slack_wake_emoji", None) or "").split(",") if name]


def clean_wake_emoji(given: list[str]) -> tuple[list[str], str | None]:
    """What a person typed, as the names Slack's reaction events carry: no
    colons, lower case, no skin tone, each once. The second value is the
    sentence to show when the list cannot be taken."""
    names: list[str] = []
    for raw in given:
        for piece in re.split(r"[,\s]+", raw):
            name = piece.strip().strip(":").split("::", 1)[0].lower()
            if not name:
                continue
            name = _EMOJI_ALIASES.get(name, name)
            if not _EMOJI_NAME.match(name):
                return [], (
                    f"{piece.strip()!r} is not an emoji name. Use the name Slack shows "
                    "for the emoji, like ticket or rotating_light."
                )
            if name in _STATUS_EMOJI:
                return [], (
                    "Agents leave eyes, white_check_mark, x and octagonal_sign on the "
                    "messages they answer, so those cannot wake one. Pick another emoji."
                )
            if name not in names:
                names.append(name)
    if len(names) > WAKE_EMOJI_MAX:
        return [], f"Pick at most {WAKE_EMOJI_MAX} emoji."
    return names, None


def effective_wake(agent) -> str:
    """The agent's mode, with NULL (every row from before the choice) read
    as `mention`: the behaviour those agents have always had."""
    mode = getattr(agent, "slack_wake", None)
    return mode if mode in WAKE_MODES else WAKE_MENTION


def wake_settings(mode: str, emoji: str, public: bool, bots: str) -> dict[str, str | None]:
    """How Slack wakes an agent, as the variables its code reads. None is a
    variable that is not set: no emoji wake it, its slash command stays
    where it has been added, or another app's mention gets knowledge-only
    tools."""
    return {
        WAKE_ENV: mode,
        WAKE_EMOJI_ENV: emoji or None,
        COMMAND_PUBLIC_ENV: COMMAND_PUBLIC_ON if public else None,
        BOT_MENTIONS_ENV: None if bots == BOT_MENTIONS_KNOWLEDGE else bots,
    }


def wake_settings_of(agent) -> dict[str, str | None]:
    return wake_settings(
        effective_wake(agent), ",".join(wake_emoji(agent)), command_public(agent), bot_mentions(agent)
    )


def wake_subscribes_channel_messages(mode: str) -> bool:
    """Whether a mode's manifest carries the two channel-message events.
    Moving across this line is the one change that needs the manifest
    re-applied in Slack; moving between `thread` and `channel` does not."""
    return mode != WAKE_MENTION


def wake_events(mode: str) -> list[str]:
    events = list(_BOT_EVENTS)
    if wake_subscribes_channel_messages(mode):
        # After app_mention, before the DM events: the order Slack lists them.
        events[1:1] = _CHANNEL_MESSAGE_EVENTS
    return events


# Clicks and the slash command (issue #484, 2026-11-14). Slack sends neither as
# an event: a click on a button or a menu goes to the app's interactivity
# address, `/the-agents-name some words` to the command's own. Both hang off
# the app's relay address (`kobo_api.slack_relay` serves them), so a manifest
# that knows the one knows the other two. An app on Socket Mode names no
# address: Slack sends both down the socket.
ACTIONS_SUFFIX = "/actions"
COMMANDS_SUFFIX = "/commands"

# Slack's own commands: an app's command by one of these names is never
# reached, so an agent that shares one simply has no command.
_SLACK_COMMANDS = frozenset({
    "active", "apps", "archive", "away", "call", "collapse", "dnd", "expand", "feed", "feedback", "giphy",
    "huddle", "invite", "join", "kick", "leave", "me", "msg", "mute", "open", "poll", "prefs", "remind",
    "remove", "rename", "search", "shortcuts", "shrug", "star", "status", "topic", "who",
})
_COMMAND_NAME = re.compile(r"^[a-z0-9_-]{1,31}$")


def slash_command(agent_name: str) -> str | None:
    """The agent's slash command, `/its-name`, or None when Slack would not
    take the name (its rule: lower case, digits, `-` and `_`, 32 characters
    with the slash) or already uses it itself."""
    name = agent_name.strip().lower()
    if not _COMMAND_NAME.match(name) or name in _SLACK_COMMANDS:
        return None
    return f"/{name}"


def bot_scopes() -> list[str]:
    """The bot scopes every manifest carries, for the install link the relay
    flow builds (issue #371): `oauth/v2/authorize` wants them spelled out."""
    return list(_BOT_SCOPES)


def scopes_owed(granted: str | None) -> list[str]:
    """The scopes a room-made app was not installed with, in manifest order.
    `granted` is Slack's own comma-separated answer at install; None is an
    app installed before the room kept that answer, which owes whatever this
    release added, and nobody can say what else, so it owes the lot."""
    if granted is None:
        return list(_BOT_SCOPES)
    held = {scope.strip() for scope in granted.split(",") if scope.strip()}
    return [scope for scope in _BOT_SCOPES if scope not in held]


def render_manifest(
    agent_name: str,
    persona_first_line: str,
    *,
    wake: str = WAKE_MENTION,
    request_url: str | None = None,
    redirect_url: str | None = None,
) -> dict:
    """Render a Slack app-manifest dict for `agent_name`.

    `request_url` (issue #371, the relay flow) switches the manifest from
    Socket Mode to events over HTTPS: `settings.event_subscriptions.
    request_url` is the room's relay address for this app and
    `socket_mode_enabled` is false, so no app-level token is ever made.
    `redirect_url` is where Slack sends the person after Install to Slack
    (the room's own callback); it is listed under `oauth_config` because
    the install exchange must name a redirect the app allows.

    `wake` picks the event list: `mention` subscribes to app_mention and the
    DM events only; `thread` and `channel` add message.channels and
    message.groups, because the runtime cannot follow a thread Slack never
    delivers. Scopes are the same in every mode (channel history was always
    granted), so changing mode never needs a reinstall, only a re-applied
    manifest.

    Interactivity is on and the agent has a slash command by its own name
    (`slash_command`; none when Slack would refuse the name). With a
    `request_url` both name an address under it (`ACTIONS_SUFFIX`,
    `COMMANDS_SUFFIX`); on Socket Mode neither names one. `should_escape`
    makes Slack send a person or channel named in a command as its id.

    Display name and bot display name are both `agent_name`. `description`
    is `persona_first_line`, stripped and truncated to Slack's 100-char
    limit (the caller — `kobo_api.main`'s slack/app route — resolves that
    string from the persona catalog; see that call site for the exact
    source and its fallback).
    """
    description = persona_first_line.strip()[:_DESCRIPTION_MAX_LEN]

    oauth_config: dict = {"scopes": {"bot": list(_BOT_SCOPES)}}
    if redirect_url:
        oauth_config["redirect_urls"] = [redirect_url]
    event_subscriptions: dict = {"bot_events": wake_events(wake)}
    interactivity: dict = {"is_enabled": True}
    if request_url:
        event_subscriptions = {"request_url": request_url, **event_subscriptions}
        interactivity["request_url"] = f"{request_url}{ACTIONS_SUFFIX}"
    features: dict = {
        "bot_user": {
            "display_name": agent_name,
            "always_online": True,
        },
    }
    command = slash_command(agent_name)
    if command:
        entry = {
            "command": command,
            "description": f"Ask {agent_name}",
            "usage_hint": "what you want to ask",
            "should_escape": True,
        }
        if request_url:
            entry["url"] = f"{request_url}{COMMANDS_SUFFIX}"
        features["slash_commands"] = [entry]

    return {
        "display_information": {
            "name": agent_name,
            "description": description,
            "background_color": _BACKGROUND_COLOR,
        },
        "features": features,
        "oauth_config": oauth_config,
        "settings": {
            "event_subscriptions": event_subscriptions,
            "interactivity": interactivity,
            "org_deploy_enabled": False,
            "socket_mode_enabled": not request_url,
            "token_rotation_enabled": False,
        },
    }


# --- wire_slack: secret delivery + repo transport wiring -----------------------

# Same cadence as kobo_api.steps's gojira-active poll (its POLL_INTERVAL_S/
# POLL_TIMEOUT_S), duplicated here as two small local constants rather than
# imported: `kobo_api.steps` is outside this task's file scope, and its own
# `_wait_active` takes a pipeline `Ctx` this module never has (only a bare
# `gojira` client and an app id) — see `wait_active`'s docstring below for
# the full reasoning versus refactoring steps.py into a shared helper.
_POLL_INTERVAL_S = 5
_POLL_TIMEOUT_S = 300

# Module-level sleep seam, same idiom as kobo_api.steps: tests monkeypatch
# this to a no-op so a 300s/5s poll loop never actually blocks.
_sleep = time.sleep


def wait_active(gojira: GojiraClient, app_id: int) -> None:
    """Poll `gojira.app_status(app_id)` every `_POLL_INTERVAL_S`s, up to
    `_POLL_TIMEOUT_S`s total, until it reports `active`; raise `ClientError`
    on `failed` (naming the failed steps) or on exhausting every poll
    without either.

    Raises `ClientError`, not bare `RuntimeError`, even though this is a
    local poll loop rather than a platform API call: `connect_slack` (the
    only caller, via `wire_slack`) catches only `ClientError` to map onto
    its plain 502, and `ClientError` subclasses `RuntimeError` (not the
    reverse) — a bare `RuntimeError` here would fall through that `except`
    and reach the route as an uncaught exception.

    A local counterpart to `kobo_api.steps._wait_active` (identical
    polling core and cadence), not a shared import: that helper takes a
    pipeline `Ctx`, which `wire_slack` doesn't have, and `kobo_api.steps`
    is outside this task's allowed file set, so it can't be refactored into
    a shared function without touching a file this task isn't scoped to
    modify. Deliberate small duplication of two constants and this loop;
    disclosed in the task report. (`steps.py`'s own `_wait_active` keeps
    raising bare `RuntimeError` — its caller's broad `except` already
    handles that, and it's out of this task's file scope.)
    """
    max_polls = _POLL_TIMEOUT_S // _POLL_INTERVAL_S
    status_payload: dict = {}
    for poll_count in range(max_polls):
        status_payload = gojira.app_status(app_id)
        status = status_payload.get("status")

        if status == "active":
            return

        if status == "failed":
            failed_names = [
                step["name"]
                for step in status_payload.get("steps", [])
                if step.get("status") == "failed"
            ]
            names = ", ".join(failed_names) if failed_names else "unknown"
            raise ClientError(
                f"gojira provisioning failed for app {app_id}: failed steps: {names}"
            )

        # Only sleep if another poll will follow
        if poll_count < max_polls - 1:
            _sleep(_POLL_INTERVAL_S)

    raise ClientError(
        f"timed out waiting for gojira app {app_id} to become active after "
        f"{_POLL_TIMEOUT_S}s (last status: {status_payload.get('status')!r})"
    )


def _update_agent_yaml_transports(agent_yaml_path: Path, channels: list[str], *, every_channel: bool = False) -> None:
    """Round-trip `bundle/agent.yaml`'s `transports` list: replace every
    existing `{kind: slack, ...}` entry with one `{"kind": "slack",
    "channel": c}` entry per channel in `channels`, order preserved. Every
    other transport (the `a2a` entry every bundle ships with) is left
    untouched, in its original position — this never replaces the list
    wholesale. An empty `channels` list disconnects: no slack entries
    remain (an agent can be wired to zero channels, e.g. mid-rewire), unless
    `every_channel` is set: then one `{"kind": "slack"}` entry with no channel
    is written, which the runtime reads as "answer in every channel the app is
    invited to". The room-made app (issue #371) is wired that way at Create,
    before any channel is picked, so its events route exists from the first
    start; picking channels later narrows it as usual.

    Every entry is written `optional: true`: the runtime serves Slack only once
    a token is in the pod's environment, and starts without it otherwise. The
    room-made app is wired at Create and its bot token arrives only when someone
    installs it, so a required entry restarted the agent into a crash loop until
    then (an agent built as an image, born without the dormant block below).

    `yaml.safe_load`/`safe_dump` round-tripping (not comment-preserving) is
    fine here, same reasoning as `kobo_api.steps._update_gojira_yaml`:
    nothing reads this file's comments, and gojira composes the runtime
    contract from parsed values, not prose.
    """
    data = yaml.safe_load(agent_yaml_path.read_text()) or {}
    transports = [t for t in data.get("transports", []) if t.get("kind") != "slack"]
    for channel in channels:
        transports.append({"kind": "slack", "channel": channel, "optional": True})
    if not channels and every_channel:
        transports.append({"kind": "slack", "optional": True})
    data["transports"] = transports
    agent_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))


def _update_gojira_secrets_from(gojira_yaml_path: Path, name: str) -> None:
    """Round-trip `gojira.yaml`'s `secrets_from` list: append `{name}-slack`
    if it isn't already there, NEVER replacing whatever's already in the
    list. `secrets_from` is a generator field the deploy platform reads to
    build the Deployment's `envFrom` — this is exactly why `wire_slack`
    below reprovisions after a push that changes it (the plan's self-review
    CORRECTION).
    """
    data = yaml.safe_load(gojira_yaml_path.read_text()) or {}
    entry = f"{name}-slack"
    if entry in (data.get("secrets_from_optional") or []):
        return  # referenced at birth as optional (tenishai/kobo#434): the pod reads it once it exists
    secrets_from = data.get("secrets_from", [])
    if entry not in secrets_from:
        secrets_from.append(entry)
    data["secrets_from"] = secrets_from
    gojira_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))


def wire_slack(
    github: GitHubClient,
    kubectl: KubectlClient,
    gojira: GojiraClient,
    *,
    repo: str,
    name: str,
    gojira_app_id: int,
    channels: list[str],
    bot_token: str,
    app_token: str,
    runtime_image: str,
    sero: SeroClient | None = None,
    acting_email: str | None = None,
    on_repin: Callable[[], None] | None = None,
    allow_tools: bool = False,
) -> str:
    """Deliver `bot_token`/`app_token` to the agent's namespace and wire its
    repo's Slack transports, mirroring `kobo_api.steps.WireStep` end to end:

    1. `kubectl.apply_secret` upserts `{name}-slack` in namespace `name` with
       `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` — the only place either token
       value is ever used; neither is written to a repo file or returned.
       When the secrets platform's machine lane is configured (`sero`,
       `Clients.sero`), that write goes THROUGH the platform instead:
       stored at `apps/{name}/slack` against `acting_email` (the person
       wiring Slack right now, else the agent's creator) and delivered into
       the agent's namespace as the same `{name}-slack` Secret, so step 2's
       `secrets_from` entry and the pod's contract are unchanged. What DOES
       change is the timing: that delivery is eventually consistent, not
       immediate. The value is stored at once, but the cluster only carries
       it after the platform next re-reads the path (the refresh interval
       this room asks for, minutes rather than the platform's hour-long
       default) -- so on a RE-wire, a pod that restarts inside that window
       can still be holding the previous tokens, where the direct write
       above had them in place before this call returned. Never both paths
       and never a fallback between them: two writers for one Secret name
       is two sources of truth, so a lane failure fails this call the same
       way a failed `apply_secret` does.
    2. Clone `repo` fresh; edit `bundle/agent.yaml` (transports gains one
       slack entry per channel in `channels`), `gojira.yaml` (`secrets_from`
       gains `{name}-slack`, append-if-missing), and `Dockerfile` (its `FROM` line
       is re-pinned to `runtime_image` — the CURRENT configured runtime
       image, e.g. `<registry>/daedalus-runtime:<tag>` — so the connect
       always delivers an image capable of running the transport it's
       wiring in; see `kobo_api.composer.update_dockerfile_runtime_image`'s
       docstring for the crashloop incident this fixes (and for
       `kobo_api.intake.upload_intake_file`, the sibling incident it was
       hoisted to also fix). A no-op when the Dockerfile already names
       `runtime_image`.
    3. `commit_push("kobo: connect slack")`. If that push actually changed
       something, `gojira.reprovision(gojira_app_id)` so the platform
       regenerates the Deployment with the new `secrets_from` entry. Either
       way, `wait_active` confirms gojira is `active` before returning — the
       same crash-window rationale `WireStep` documents (a previous attempt
       may have crashed between `reprovision()` and its own wait).

    Returns `github.local_head(workdir)` — the just-pushed clone's own HEAD,
    not a fresh API read (the P2b lesson: a GitHub API read can lag a
    moment behind a push this same process just made).

    Note this signature carries `gojira`/`gojira_app_id` beyond the task
    brief's original sketch — the plan's self-review CORRECTION requires a
    reprovision-called/skipped matrix to be asserted at this function's own
    level, so the gojira client and app id must be inputs here rather than
    only available to the route that calls this. Disclosed in the task
    report.
    """
    return wire_slack_values(
        github,
        kubectl,
        gojira,
        repo=repo,
        name=name,
        gojira_app_id=gojira_app_id,
        channels=channels,
        values={"SLACK_BOT_TOKEN": bot_token, "SLACK_APP_TOKEN": app_token},
        allow_tools=allow_tools,
        runtime_image=runtime_image,
        sero=sero,
        acting_email=acting_email,
        on_repin=on_repin,
    )


def deliver_slack_values(
    kubectl: KubectlClient,
    *,
    name: str,
    values: dict[str, str],
    sero: SeroClient | None = None,
    acting_email: str | None = None,
    remove_keys: Sequence[str] = (),
) -> None:
    """The Secret half of the wiring: the app's values into `<name>-slack`,
    through the secrets lane when the room has one, else written whole. On
    its own because a connected agent takes this half as it is and the
    repository half as a pull request (`kobo_api.connected_wiring`)."""
    if sero is not None:
        deliver_secret(
            sero,
            acting_email=acting_email,
            agent_name=name,
            purpose="slack",
            values=values,
            secret_name=f"{name}-slack",
            remove=list(remove_keys) or None,
        )
    else:
        kubectl.apply_secret(name, f"{name}-slack", values)


def commit_slack_reading(
    github: GitHubClient, repo: str, *, on: bool, redeliver: bool = False
) -> tuple[str, bool]:
    """Clone `repo`, turn its bundle's Slack reading lane on or off, commit
    and push. Returns the head and whether the running agent is restarting
    with it: false in a room that builds each change, and for a bundle that
    already says so, which pushes nothing.

    `redeliver` is for that last case when the room's own record disagrees
    with the file: an earlier save whose commit landed and whose delivery
    failed, or a block somebody wrote by hand. The bundle is published as it
    stands and the agent restarted, so asking again repairs the agent and
    not only the repository. Raises `ClientError` on any GitHub, agent.yaml
    or delivery failure."""
    with repo_write_lock(repo), tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        update_agent_yaml_slack_read(workdir / "bundle", on=on)
        pushed = delivery.push(
            github, workdir,
            "kobo: in Slack, it reads the channels it is in" if on else "kobo: in Slack, it no longer reads channels",
        )
        delivered = pushed and delivery.configured()
        if not pushed and redeliver:
            delivered = delivery.redeliver(github, workdir)
        return github.local_head(workdir), delivered


def deliver_wake_settings(
    kubectl: KubectlClient,
    *,
    name: str,
    settings: dict[str, str | None],
    sero: SeroClient | None = None,
    acting_email: str | None = None,
) -> None:
    """How Slack wakes the agent, into `<name>-slack` beside the app's values,
    which stay as they are. For an agent that runs its team's own code: the
    room cannot commit to its repository, so a setting kept in its files is
    a pull request each time it changes, and one kept here is a restart."""
    values = {key: value for key, value in settings.items() if value is not None}
    gone = [key for key, value in settings.items() if value is None]
    if sero is not None:
        deliver_secret(
            sero,
            acting_email=acting_email,
            agent_name=name,
            purpose="slack",
            values=values,
            secret_name=f"{name}-slack",
            remove=gone or None,
        )
        return
    # Written back whole, so a read that failed must stop this: taken for an
    # empty Secret it would write away the app's own values.
    existing = kubectl.secret_literals(name, f"{name}-slack", missing_ok=True)
    kept = {key: value for key, value in existing.items() if key not in gone}
    kubectl.apply_secret(name, f"{name}-slack", {**kept, **values})


def wire_slack_values(
    github: GitHubClient,
    kubectl: KubectlClient,
    gojira: GojiraClient,
    *,
    repo: str,
    name: str,
    gojira_app_id: int,
    channels: list[str],
    values: dict[str, str],
    runtime_image: str,
    sero: SeroClient | None = None,
    acting_email: str | None = None,
    on_repin: Callable[[], None] | None = None,
    edit_gojira: Callable[[dict], bool] | None = None,
    message: str = "kobo: connect slack",
    every_channel: bool = False,
    remove_keys: Sequence[str] = (),
    allow_tools: bool = False,
) -> str:
    """`wire_slack` over any set of Slack values (issue #371): the relay flow
    delivers the app's five credentials at Create (no app-level token
    exists), the paste flow its two tokens. Same Secret name, same path,
    same repository edits and the same reprovision-and-wait; `edit_gojira`,
    when given, is one more edit of the parsed `gojira.yaml` in the same
    clone (the relay flow writes the wake mode it was created with), so the
    choice and the wiring land in one commit.

    `remove_keys` names values the same delivery takes out of the Secret
    (the move from Socket Mode to the relay, issue #304: the app-level token
    and the old bot token go as the new app's five values arrive, so the
    runtime takes the HTTP way in at its next start). Through the lane that
    is one `put` with `remove`; the direct write replaces the Secret's data
    with `values` whole, so anything not named is gone by construction.

    `allow_tools` opens the Slack tools (`SLACK_TOOLS_ALLOW`, issue #484) in
    the same commit, for an agent born before the room composed them. The
    caller says so: it knows whether this is an agent that may act."""
    deliver_slack_values(
        kubectl, name=name, values=values, sero=sero, acting_email=acting_email, remove_keys=remove_keys
    )

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)

        agent_yaml_path = workdir / "bundle" / "agent.yaml"
        if not agent_yaml_path.exists():
            # A repository without the bundle is not one the room composed;
            # say so as a ClientError (the routes' 502) rather than crash.
            raise ClientError(f"{repo} has no bundle/agent.yaml to wire Slack into")
        _update_agent_yaml_transports(agent_yaml_path, channels, every_channel=every_channel)
        if allow_tools:
            try:
                update_agent_yaml_allow(workdir / "bundle", entries=[SLACK_TOOLS_ALLOW])
            except ClientError as exc:
                # No tools section to open them in: the wiring still lands.
                log.info("%s: the Slack tools were not opened: %s", name, exc)
        gojira_yaml_path = workdir / "gojira.yaml"
        contract_before = gojira_yaml_path.read_text() if gojira_yaml_path.exists() else ""
        _update_gojira_secrets_from(gojira_yaml_path, name)
        if edit_gojira is not None:
            data = yaml.safe_load(gojira_yaml_path.read_text()) or {}
            if edit_gojira(data):
                gojira_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))
        contract_changed = (gojira_yaml_path.read_text() if gojira_yaml_path.exists() else "") != contract_before
        previous = None
        note = ""
        if not delivery.configured():
            # With bundles as artifacts the runtime route is the one place a pin moves.
            previous = update_dockerfile_runtime_image(workdir / "Dockerfile", runtime_image)
            note = repin_commit_note(repo, previous, runtime_image)
        by_restart = delivery.configured() and not contract_changed

        if by_restart:
            # The Secret is written and the transport is in the bundle (born dormant): a
            # restart delivers it, no build and no reprovision. Through the platform the
            # values land at its next re-read, so the restart waits for them, off this
            # request; a direct write is there already and the restart follows at once.
            delivery.push(github, workdir, f"{message}{note}", restart=False)
            delivery.restart_once_secret_carries(kubectl, name, f"{name}-slack", list(values))
            changed = False
        else:
            changed = delivery.push(github, workdir, f"{message}{note}")
        if changed:
            gojira.reprovision(gojira_app_id)
        # Always confirm active, even on the no-change path — see the
        # docstring above and kobo_api.steps.WireStep's own comment for
        # why this can't be skipped just because nothing was pushed.
        if not by_restart:
            wait_active(gojira, gojira_app_id)

        head = github.local_head(workdir)
    # The card now names a new runtime (issue #147); the caller re-registers it.
    if previous is not None and on_repin is not None:
        on_repin()
    return head


# The tools backfill's tries, and the pause before each later one (times its number).
_ALLOW_TRIES = 3
_ALLOW_PAUSE = 5.0


def allow_slack_tools(github: GitHubClient, *, repo: str, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Make an agent's own bundle admit the Slack tools (`SLACK_TOOLS_ALLOW`):
    clone `repo`, add the entry when no pattern there covers it, publish. An
    agent born since the entry was composed already has it, and then nothing
    is pushed. No restart of its own: the caller is on its way to one (the
    install callback restarts the agent for its token). Returns whether the
    bundle changed. Raises `ClientError`.

    The clone and the push are tried `_ALLOW_TRIES` times, since nothing asks
    for this a second time once the app holds its scopes. A try that finds the
    entry already there after a failed push publishes what the repository
    holds: the commit landed and its delivery did not."""
    failed: ClientError | None = None
    for attempt in range(_ALLOW_TRIES):
        if attempt:
            sleep(_ALLOW_PAUSE * attempt)
        with repo_write_lock(repo), tempfile.TemporaryDirectory(prefix="kobo-slack-allow-") as tmp:
            workdir = Path(tmp) / "repo"
            try:
                github.clone(repo, workdir)
            except ClientError as exc:
                failed = exc
                continue
            changed = update_agent_yaml_allow(workdir / "bundle", entries=[SLACK_TOOLS_ALLOW])
            if not changed and failed is None:
                return False
            try:
                if changed:
                    delivery.push(github, workdir, "kobo: allow the Slack tools", restart=False)
                elif (agent_name := delivery.agent_name_of(workdir)) is not None:
                    delivery.publish(workdir, agent_name, head_sha=github.local_head(workdir) or "")
            except ClientError as exc:
                failed = exc
                continue
            return True
    assert failed is not None
    raise failed


def rewire_slack_channels(
    github: GitHubClient, *, repo: str, channels: list[str], every_channel: bool = False
) -> str:
    """Update which channels an already-connected agent listens on: clone
    `repo` fresh, replace `bundle/agent.yaml`'s slack transports with one
    entry per channel in `channels` (order preserved; `_update_agent_yaml_
    transports` handles the round-trip), commit as `"kobo: slack channels
    updated"`, and push. `every_channel` is the room-made app's case (issue
    #371): an empty list writes the one open entry back instead of none, so
    clearing the list returns the agent to answering wherever it is invited
    rather than going silent.

    Commit-only, deliberately smaller than `wire_slack` above: rewiring
    channels never touches a bot/app token, `gojira.yaml`'s `secrets_from`,
    or the Dockerfile's runtime image, so there's nothing here for
    `kubectl` or `gojira` to do — no secret delivery, no reprovision, no
    `wait_active` poll. This is why the signature carries neither client:
    reusing `wire_slack`'s clone/commit/push path (`github.clone`,
    `github.commit_push`, `github.local_head`) is exactly reuse, not
    duplication, since those three calls are already `GitHubClient`
    primitives rather than logic private to `wire_slack` — nothing about
    them is tangled with the secret/reprovision mechanics that stay behind
    in `wire_slack` alone.

    Returns `github.local_head(workdir)` — the same "just-pushed local
    clone's own HEAD" convention `wire_slack` returns (see its docstring
    for why: a fresh GitHub API read can lag behind a push this same
    process just made).
    """
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)

        _update_agent_yaml_transports(workdir / "bundle" / "agent.yaml", channels, every_channel=every_channel)

        delivery.push(github, workdir, "kobo: slack channels updated")

        return github.local_head(workdir)
