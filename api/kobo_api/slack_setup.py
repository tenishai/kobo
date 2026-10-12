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

import tempfile
import time
from pathlib import Path

import yaml

from kobo_api.clients import ClientError, GitHubClient, GojiraClient, KubectlClient
from kobo_api.composer import update_dockerfile_runtime_image
from kobo_api.sero import SeroClient, deliver_secret

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
    # Vision (wave 3): downloading a message's image attachments via
    # files.url_private requires this read scope. A SCOPE addition --
    # unlike the tagged-only event change -- requires each existing
    # agent's Slack app to be reinstalled to its workspace after the
    # manifest re-apply; new agents get it from this generator.
    "files:read",
    "reactions:read",
    "reactions:write",
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


def effective_wake(agent) -> str:
    """The agent's mode, with NULL (every row from before the choice) read
    as `mention`: the behaviour those agents have always had."""
    mode = getattr(agent, "slack_wake", None)
    return mode if mode in WAKE_MODES else WAKE_MENTION


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


def render_manifest(agent_name: str, persona_first_line: str, *, wake: str = WAKE_MENTION) -> dict:
    """Render a Slack app-manifest dict for `agent_name`.

    `wake` picks the event list: `mention` subscribes to app_mention and the
    DM events only; `thread` and `channel` add message.channels and
    message.groups, because the runtime cannot follow a thread Slack never
    delivers. Scopes are the same in every mode (channel history was always
    granted), so changing mode never needs a reinstall, only a re-applied
    manifest.

    Display name and bot display name are both `agent_name`. `description`
    is `persona_first_line`, stripped and truncated to Slack's 100-char
    limit (the caller — `kobo_api.main`'s slack/app route — resolves that
    string from the persona catalog; see that call site for the exact
    source and its fallback).
    """
    description = persona_first_line.strip()[:_DESCRIPTION_MAX_LEN]

    return {
        "display_information": {
            "name": agent_name,
            "description": description,
            "background_color": _BACKGROUND_COLOR,
        },
        "features": {
            "bot_user": {
                "display_name": agent_name,
                "always_online": True,
            },
        },
        "oauth_config": {
            "scopes": {
                "bot": list(_BOT_SCOPES),
            },
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": wake_events(wake),
            },
            "interactivity": {"is_enabled": False},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
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


def _update_agent_yaml_transports(agent_yaml_path: Path, channels: list[str]) -> None:
    """Round-trip `bundle/agent.yaml`'s `transports` list: replace every
    existing `{kind: slack, ...}` entry with one `{"kind": "slack",
    "channel": c}` entry per channel in `channels`, order preserved. Every
    other transport (the `a2a` entry every bundle ships with) is left
    untouched, in its original position — this never replaces the list
    wholesale. An empty `channels` list disconnects: no slack entries
    remain (an agent can be wired to zero channels, e.g. mid-rewire).

    `yaml.safe_load`/`safe_dump` round-tripping (not comment-preserving) is
    fine here, same reasoning as `kobo_api.steps._update_gojira_yaml`:
    nothing reads this file's comments, and gojira composes the runtime
    contract from parsed values, not prose.
    """
    data = yaml.safe_load(agent_yaml_path.read_text()) or {}
    transports = [t for t in data.get("transports", []) if t.get("kind") != "slack"]
    transports.extend({"kind": "slack", "channel": channel} for channel in channels)
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
    secrets_from = data.get("secrets_from", [])
    entry = f"{name}-slack"
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
    tokens = {"SLACK_BOT_TOKEN": bot_token, "SLACK_APP_TOKEN": app_token}
    if sero is not None:
        deliver_secret(
            sero,
            acting_email=acting_email,
            agent_name=name,
            purpose="slack",
            values=tokens,
            secret_name=f"{name}-slack",
        )
    else:
        kubectl.apply_secret(name, f"{name}-slack", tokens)

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)

        _update_agent_yaml_transports(workdir / "bundle" / "agent.yaml", channels)
        _update_gojira_secrets_from(workdir / "gojira.yaml", name)
        update_dockerfile_runtime_image(workdir / "Dockerfile", runtime_image)

        changed = github.commit_push(workdir, "kobo: connect slack")
        if changed:
            gojira.reprovision(gojira_app_id)
        # Always confirm active, even on the no-change path — see the
        # docstring above and kobo_api.steps.WireStep's own comment for
        # why this can't be skipped just because nothing was pushed.
        wait_active(gojira, gojira_app_id)

        return github.local_head(workdir)


def rewire_slack_channels(github: GitHubClient, *, repo: str, channels: list[str]) -> str:
    """Update which channels an already-connected agent listens on: clone
    `repo` fresh, replace `bundle/agent.yaml`'s slack transports with one
    entry per channel in `channels` (order preserved; `_update_agent_yaml_
    transports` handles the round-trip), commit as `"kobo: slack channels
    updated"`, and push.

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

        _update_agent_yaml_transports(workdir / "bundle" / "agent.yaml", channels)

        github.commit_push(workdir, "kobo: slack channels updated")

        return github.local_head(workdir)
