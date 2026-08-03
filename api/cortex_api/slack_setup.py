"""Slack connect (P2c): app-manifest rendering, plus the wire flow that
delivers pasted Slack tokens to the agent's namespace and threads the
channel/secrets contract through its repo.

Two independent responsibilities live here:

- `render_manifest` builds the Slack app manifest cortex hands to
  `SlackClient.create_app` (cortex_api.clients, Task 2). Its scopes/events
  are a deliberate SUPERSET of the runtime kit's canonical template
  (`docs/slack-app-manifest.yaml` in the daedalus repo) — public-channel
  grants plus private-channel grants (`groups:history`/`message.groups`)
  together, not a straight copy of one or the other. See the P2c incident-1
  writeup (`docs/superpowers/plans/p0-e2e-runbook.md`, "P2c results — slack
  connect live") for why: a live connect crashed on a private channel
  because the manifest only carried public-channel grants, fixed by
  rendering both grant sets so cortex never has to guess a channel's type
  up front.

- `wire_slack` mirrors `cortex_api.steps.WireStep`'s pattern verbatim (clone
  -> edit YAMLs -> commit_push -> reprovision-if-changed -> wait-active):
  it delivers the bot/app tokens to the cluster as a namespace Secret, edits
  `bundle/agent.yaml`'s transports list and `gojira.yaml`'s `secrets_from`
  list in a fresh clone, pushes, and — the plan's self-review CORRECTION —
  reprovisions gojira when that push actually changed something, since
  `secrets_from` is a generator field the deploy platform reads to build the
  Deployment's envFrom. It always confirms gojira reaches `active` before
  returning, even on the no-change path, for the exact crash-window reason
  `WireStep` documents: a prior attempt may have crashed after calling
  `reprovision()` but before its own wait finished.
"""

from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path

import yaml

from cortex_api.clients import ClientError, GitHubClient, GojiraClient, KubectlClient

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
# public-channel half of this list — rendering both grant sets means cortex
# never has to know a channel's type before connecting it.
_BOT_SCOPES = [
    "app_mentions:read",
    "channels:history",
    "groups:history",
    "chat:write",
    "reactions:read",
    "reactions:write",
]

_BOT_EVENTS = ["app_mention", "message.channels", "message.groups"]


def render_manifest(agent_name: str, persona_first_line: str) -> dict:
    """Render a Slack app-manifest dict for `agent_name`.

    Display name and bot display name are both `agent_name`. `description`
    is `persona_first_line`, stripped and truncated to Slack's 100-char
    limit (the caller — `cortex_api.main`'s slack/app route — resolves that
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
                "bot_events": list(_BOT_EVENTS),
            },
            "interactivity": {"is_enabled": False},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


# --- wire_slack: secret delivery + repo transport wiring -----------------------

# Same cadence as cortex_api.steps's gojira-active poll (its POLL_INTERVAL_S/
# POLL_TIMEOUT_S), duplicated here as two small local constants rather than
# imported: `cortex_api.steps` is outside this task's file scope, and its own
# `_wait_active` takes a pipeline `Ctx` this module never has (only a bare
# `gojira` client and an app id) — see `wait_active`'s docstring below for
# the full reasoning versus refactoring steps.py into a shared helper.
_POLL_INTERVAL_S = 5
_POLL_TIMEOUT_S = 300

# Module-level sleep seam, same idiom as cortex_api.steps: tests monkeypatch
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

    A local counterpart to `cortex_api.steps._wait_active` (identical
    polling core and cadence), not a shared import: that helper takes a
    pipeline `Ctx`, which `wire_slack` doesn't have, and `cortex_api.steps`
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


def _update_agent_yaml_transport(agent_yaml_path: Path, channel: str) -> None:
    """Round-trip `bundle/agent.yaml`'s `transports` list: replace an
    existing `{kind: slack, ...}` entry in place (idempotent re-wire with a
    new channel), or append a new one if there isn't one yet. Every other
    transport (the `a2a` entry every bundle ships with) is left untouched,
    in its original position — this never replaces the list wholesale.

    `yaml.safe_load`/`safe_dump` round-tripping (not comment-preserving) is
    fine here, same reasoning as `cortex_api.steps._update_gojira_yaml`:
    nothing reads this file's comments, and gojira composes the runtime
    contract from parsed values, not prose.
    """
    data = yaml.safe_load(agent_yaml_path.read_text()) or {}
    transports = data.get("transports", [])
    new_entry = {"kind": "slack", "channel": channel}

    updated = []
    replaced = False
    for entry in transports:
        if isinstance(entry, dict) and entry.get("kind") == "slack":
            updated.append(new_entry)
            replaced = True
        else:
            updated.append(entry)
    if not replaced:
        updated.append(new_entry)

    data["transports"] = updated
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


# Matches a Dockerfile's `FROM <image>` line (composer's own template,
# `cortex_api.composer._dockerfile`, writes exactly one) anywhere in the
# file, not just its first line — a leading comment line (as that template
# has) is not itself a FROM line. Captures the `FROM` keyword plus the
# whitespace that follows it, so the replacement can preserve that prefix
# byte-for-byte and only swap the image-ref token itself.
_DOCKERFILE_FROM_RE = re.compile(r"^(FROM[ \t]+)\S+", re.MULTILINE)


def _update_dockerfile_runtime_image(dockerfile_path: Path, runtime_image: str) -> None:
    """Rewrite the clone's `Dockerfile` so its `FROM` line's image ref is
    exactly `runtime_image`, leaving every other line (and any trailing
    content on the FROM line itself) byte-identical.

    This is the runtime-bump incident fix: `cortex_api.composer` bakes the
    runtime image into a repo's Dockerfile once, at compose time
    (`FROM <registry>/daedalus-runtime:<tag>`). A later slack connect that
    only edited `bundle/agent.yaml`'s transports left that pinned image
    untouched — when the image predated the slack transport's pip extra,
    the runtime raised `BundleError("transport 'slack' requires the slack
    extra")` at boot and the agent crashlooped, taking its a2a chat down
    with it. Every `wire_slack` call now also re-pins the FROM line to the
    CURRENTLY configured runtime image, so a connect always delivers an
    image capable of running the transport it's wiring in.

    Idempotent by construction: when the FROM line already names
    `runtime_image`, the substitution is a no-op and the rewritten text is
    byte-identical to what was already on disk — so this edit alone never
    makes `commit_push` see a diff on an already-current repo.

    Uses a replacement function (not a plain substitution string) so that
    `runtime_image` is inserted literally even if it happens to contain
    characters (like a backslash) that `re.sub`'s string-replacement syntax
    would otherwise treat specially.

    Raises `ClientError` (never silently no-ops) in the two ways this can
    fail to actually re-pin anything — final-review Finding 2:

    - The Dockerfile is missing entirely: `Path.read_text()` raises
      `OSError`, caught and re-raised as `ClientError` naming only the path,
      same idiom as `cortex_api.clients._read_slack_tokens`.
    - The Dockerfile has no line matching `_DOCKERFILE_FROM_RE` (some
      unrecognized/hand-edited Dockerfile shape): `re.sub` on zero matches
      is itself a silent no-op — it happily writes the file back unchanged
      and returns, giving no signal that the re-pin never happened. That
      used to mean the stale-base crashloop this function exists to fix
      could recur with nothing in the way of a symptom. `re.subn` (which
      reports the match count) lets this catch that here and raise instead,
      so the failure surfaces immediately as this route's 502, not as a
      crashloop discovered later with no trail back to its cause.
    """
    try:
        text = dockerfile_path.read_text()
    except OSError as exc:
        raise ClientError(f"Dockerfile not found: {dockerfile_path}") from exc

    updated, count = _DOCKERFILE_FROM_RE.subn(
        lambda match: match.group(1) + runtime_image, text, count=1
    )
    if count == 0:
        raise ClientError(
            f"Dockerfile has no recognizable FROM line to re-pin: {dockerfile_path}"
        )
    dockerfile_path.write_text(updated)


def wire_slack(
    github: GitHubClient,
    kubectl: KubectlClient,
    gojira: GojiraClient,
    *,
    repo: str,
    name: str,
    gojira_app_id: int,
    channel: str,
    bot_token: str,
    app_token: str,
    runtime_image: str,
) -> str:
    """Deliver `bot_token`/`app_token` to the agent's namespace and wire its
    repo's Slack transport, mirroring `cortex_api.steps.WireStep` end to end:

    1. `kubectl.apply_secret` upserts `{name}-slack` in namespace `name` with
       `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` — the only place either token
       value is ever used; neither is written to a repo file or returned.
    2. Clone `repo` fresh; edit `bundle/agent.yaml` (transports gains/updates
       the slack entry), `gojira.yaml` (`secrets_from` gains
       `{name}-slack`, append-if-missing), and `Dockerfile` (its `FROM` line
       is re-pinned to `runtime_image` — the CURRENT configured runtime
       image, e.g. `<registry>/daedalus-runtime:<tag>` — so the connect
       always delivers an image capable of running the transport it's
       wiring in; see `_update_dockerfile_runtime_image`'s docstring for the
       crashloop incident this fixes). A no-op when the Dockerfile already
       names `runtime_image`.
    3. `commit_push("cortex: connect slack")`. If that push actually changed
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
    kubectl.apply_secret(
        name,
        f"{name}-slack",
        {"SLACK_BOT_TOKEN": bot_token, "SLACK_APP_TOKEN": app_token},
    )

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)

        _update_agent_yaml_transport(workdir / "bundle" / "agent.yaml", channel)
        _update_gojira_secrets_from(workdir / "gojira.yaml", name)
        _update_dockerfile_runtime_image(workdir / "Dockerfile", runtime_image)

        changed = github.commit_push(workdir, "cortex: connect slack")
        if changed:
            gojira.reprovision(gojira_app_id)
        # Always confirm active, even on the no-change path — see the
        # docstring above and cortex_api.steps.WireStep's own comment for
        # why this can't be skipped just because nothing was pushed.
        wait_active(gojira, gojira_app_id)

        return github.local_head(workdir)
