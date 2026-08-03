"""Slack connect (P2c): app-manifest rendering, plus the wire flow that
delivers pasted Slack tokens to the agent's namespace and threads the
channel/secrets contract through its repo.

Two independent responsibilities live here:

- `render_manifest` builds the Slack app manifest cortex hands to
  `SlackClient.create_app` (cortex_api.clients, Task 2). Its scopes/events/
  settings are copied EXACTLY from the runtime kit's canonical template
  (`docs/slack-app-manifest.yaml` in the daedalus repo) — the runtime
  transport's own documented contract, never invented here.

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

import tempfile
import time
from pathlib import Path

import yaml

from cortex_api.clients import GitHubClient, GojiraClient, KubectlClient

# --- render_manifest -----------------------------------------------------------

# Slack's own display_information.description limit.
_DESCRIPTION_MAX_LEN = 100

# Not agent-specific; copied verbatim from the canonical template.
_BACKGROUND_COLOR = "#3d2f5b"

# Bot scopes/events/settings: copied EXACTLY from the runtime kit's canonical
# manifest (daedalus/docs/slack-app-manifest.yaml) per the plan's Global
# Constraints — the runtime transport's documented contract, not invented
# here. Keep byte-identical to that file if it ever changes.
_BOT_SCOPES = [
    "app_mentions:read",
    "channels:history",
    "chat:write",
    "reactions:read",
    "reactions:write",
]

_BOT_EVENTS = ["app_mention", "message.channels"]


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
    `_POLL_TIMEOUT_S`s total, until it reports `active`; raise on `failed`
    (naming the failed steps) or on exhausting every poll without either.

    A local counterpart to `cortex_api.steps._wait_active` (identical
    polling core and cadence), not a shared import: that helper takes a
    pipeline `Ctx`, which `wire_slack` doesn't have, and `cortex_api.steps`
    is outside this task's allowed file set, so it can't be refactored into
    a shared function without touching a file this task isn't scoped to
    modify. Deliberate small duplication of two constants and this loop;
    disclosed in the task report.
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
            raise RuntimeError(
                f"gojira provisioning failed for app {app_id}: failed steps: {names}"
            )

        # Only sleep if another poll will follow
        if poll_count < max_polls - 1:
            _sleep(_POLL_INTERVAL_S)

    raise RuntimeError(
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
) -> str:
    """Deliver `bot_token`/`app_token` to the agent's namespace and wire its
    repo's Slack transport, mirroring `cortex_api.steps.WireStep` end to end:

    1. `kubectl.apply_secret` upserts `{name}-slack` in namespace `name` with
       `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` — the only place either token
       value is ever used; neither is written to a repo file or returned.
    2. Clone `repo` fresh; edit `bundle/agent.yaml` (transports gains/updates
       the slack entry) and `gojira.yaml` (`secrets_from` gains
       `{name}-slack`, append-if-missing).
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

        changed = github.commit_push(workdir, "cortex: connect slack")
        if changed:
            gojira.reprovision(gojira_app_id)
        # Always confirm active, even on the no-change path — see the
        # docstring above and cortex_api.steps.WireStep's own comment for
        # why this can't be skipped just because nothing was pushed.
        wait_active(gojira, gojira_app_id)

        return github.local_head(workdir)
