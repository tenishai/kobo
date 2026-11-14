"""What a connected agent's page says is missing (2026-11-14).

An agent the room builds works or it does not, and the room knows which,
because the room made every part of it. An agent that runs its team's own
code can be deployed and still be half in the room: its files may not carry
the room's setup yet, or its code may not take the messages the room hands
it. Each of those used to be found by reading logs.

So the page says three things, each one sentence, in the order they have to
come true:

- `running`: the cluster's own word (`kobo_api.health`).
- `gateway`: whether its files carry the room's setup for the tool gateway
  (`connected_wiring.state`), and when the gateway last saw a call from it.
  Left out when the room has no gateway to set an agent up for.
- `slack`: where its room-made Slack app stands, and what became of the last
  message the room handed it (`slack_relay.record_outcome`). Left out when
  the room makes no Slack apps, or the agent serves no address to hand a
  message to.

Each is `{"id", "state", "line", "at", "pull_request"}`. `state` is one of
four words the page turns into a dot: `ok`, `waiting` (on a person, or on a
restart), `missing` (something is wrong and the line says what) and `idle`
(nothing to report either way). The sentences are written here, not in the
page, so a person's assistant reads the same ones. They follow the guide's
house rules: plain words, no infrastructure vocabulary.

Nothing here asks anything; the caller gathers the facts and these functions
only word them. A fact that could not be read is passed as `UNREAD` and gets
a line that claims nothing.
"""

from __future__ import annotations

from kobo_api.connected_wiring import OWED, WAITING
from kobo_api.health import DOWN
from kobo_api.slack_relay import LOST, REFUSED, TAKEN

STATE_OK = "ok"
STATE_WAITING = "waiting"
STATE_MISSING = "missing"
STATE_IDLE = "idle"

# A fact the caller asked for and could not get (the repository or the
# gateway did not answer). Never the same as "no".
UNREAD = object()

# How far back the gateway is asked for the agent's last call.
LAST_CALL_DAYS = 7


def _check(check_id: str, state: str, line: str, *, at: str | None = None, pull_request: str | None = None) -> dict:
    return {"id": check_id, "state": state, "line": line, "at": at, "pull_request": pull_request}


def running(health: dict | None) -> dict:
    """`health` is `AgentHealth.body()`, or None before the first reading."""
    if health is None:
        return _check("running", STATE_IDLE, "The room has not looked at it yet. It does once a minute.")
    if health.get("state") == DOWN:
        if health.get("rolling"):
            return _check("running", STATE_WAITING, "Restarting. Back in about a minute.")
        return _check("running", STATE_MISSING, f"Not running: {health.get('reason') or 'it did not start'}.")
    if health.get("rolling"):
        return _check("running", STATE_WAITING, "Running, and restarting with a change.")
    return _check("running", STATE_OK, "Running.", at=health.get("ready_since"))


def gateway(wiring: object, *, last_call_at: object = UNREAD) -> dict:
    """`wiring` is `connected_wiring.state`'s answer, or `UNREAD`.
    `last_call_at` is when the gateway last saw a call from the agent in
    `LAST_CALL_DAYS` days (a timestamp), None when it saw none, or `UNREAD`
    when the room cannot ask."""
    if not isinstance(wiring, dict):
        return _check("gateway", STATE_IDLE, "The room could not read its files just now.")
    if wiring.get("state") == WAITING:
        return _check(
            "gateway", STATE_WAITING,
            "Waiting on your team to merge the room's setup pull request.",
            pull_request=wiring.get("pull_request"),
        )
    if wiring.get("state") == OWED:
        return _check(
            "gateway", STATE_MISSING,
            "Its files do not carry the room's setup for the tool gateway. "
            "The room asks with a pull request once the agent is granted something.",
        )
    if last_call_at is UNREAD:
        return _check("gateway", STATE_OK, "Set up to reach the tool gateway.")
    if last_call_at is None:
        return _check(
            "gateway", STATE_IDLE,
            f"Set up to reach the tool gateway. No call from it in the last {LAST_CALL_DAYS} days.",
        )
    return _check("gateway", STATE_OK, "Reaching the tool gateway.", at=str(last_call_at))


def slack(app_state: str, wiring: object, *, outcome: str | None, at: str | None) -> dict:
    """`app_state` is the Slack row's own (`none`, `created`, `installed`);
    `wiring` is `connected_wiring.slack_state`'s answer or `UNREAD`;
    `outcome` and `at` are the app's last hand-off."""
    if app_state == "none":
        return _check("slack", STATE_IDLE, "No Slack app from the room. Add one under Access.")
    if app_state == "created":
        return _check("slack", STATE_WAITING, "Its Slack app is made and waits to be installed, under Access.")
    if isinstance(wiring, dict) and wiring.get("state") == WAITING:
        return _check(
            "slack", STATE_WAITING,
            "Waiting on your team to merge the room's Slack pull request.",
            pull_request=wiring.get("pull_request"),
        )
    if isinstance(wiring, dict) and wiring.get("state") == OWED:
        return _check(
            "slack", STATE_MISSING,
            "Its files do not read the Slack app's values. Open the Slack row under Access to ask again.",
        )
    if outcome == TAKEN:
        return _check("slack", STATE_OK, "Taking messages from Slack.", at=at)
    if outcome == REFUSED:
        return _check(
            "slack", STATE_MISSING,
            "It refused the last message from Slack. Its code has to take the messages the room hands it.",
            at=at,
        )
    if outcome == LOST:
        return _check("slack", STATE_MISSING, "The last message from Slack did not reach it.", at=at)
    if not isinstance(wiring, dict):
        return _check("slack", STATE_IDLE, "The room could not read its files just now.")
    return _check("slack", STATE_IDLE, "In Slack. No message has come for it yet.")


__all__ = [
    "LAST_CALL_DAYS",
    "STATE_IDLE",
    "STATE_MISSING",
    "STATE_OK",
    "STATE_WAITING",
    "UNREAD",
    "gateway",
    "running",
    "slack",
]
