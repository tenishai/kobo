"""Keeping an agent that runs its team's own code wired, after it is connected.

The wire step gives a connected agent everything the room has for it, once,
at connect. Two things happen later and in somebody else's repository, so
no step can do them:

Follow-through. Every change the room proposes to a connected agent is a
pull request on its `gojira.yaml`. When the team merges one, the file on the
default branch no longer matches the deploy files that were rendered from
it, and the repository's own build refuses to deploy until the platform
regenerates them. Nothing asked the platform to (found 2026-11-13, before
it bit: a secrets wiring request was waiting on a team, and merging it
would have left their build red with nobody to turn to). `follow_through`
asks, once per version of the file.

Catch-up. An agent connected before the room gave connected agents storage
and a gateway identity has neither. It is offered both, as one pull
request, the first time it holds a permission that needs the gateway: the
moment its team has asked for something that cannot work without it. No
button; an agent that holds nothing is left alone.

Both run from the worker's five-minute pass (`keep`), beside the gateway
heal, and neither ever pushes to a team's branch.

Slack. A connected agent that serves a port can have a Slack app the room
makes (issue #484). The app's values go to the Secret `<agent>-slack` the
moment the app exists; what the agent's repository owes is one reference to
that Secret and the wake mode, and that is a pull request on `kobo/slack`
(`propose_slack`), opened by the route that makes the app and again by the
one that changes the wake mode. `slack_state` is what the Slack row reads:
wired, waiting on the team, or owed because the request was closed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import tempfile
from pathlib import Path

import yaml

from kobo_api.agent_env import (
    GOJIRA_YAML,
    UNCHANGED,
    RepoBusy,
    RepoEdit,
    _propose,
    edit_gojira_yaml,
    parse_gojira_yaml,
)
from kobo_api.clients import ClientError
from kobo_api.kinds import KIND_CONNECTED
from kobo_api.models import Agent, AgentStatus
from kobo_api.slack_setup import (
    COMMAND_PUBLIC_ENV,
    COMMAND_PUBLIC_ON,
    WAKE_EMOJI_ENV,
    WAKE_ENV,
    command_public,
    effective_wake,
)
from kobo_api.steps import (
    CONNECTED_GATEWAY_URL_ENV,
    CONNECTED_STORAGE_FILE,
    CONNECTED_SWITCHBOARD_URL_ENV,
    CONNECTED_WIRE_BRANCH,
    add_room_wiring,
    connected_room_wiring_lines,
    deliver_connected_identity,
    gateway_identity_on,
    write_connected_storage,
)

log = logging.getLogger(__name__)

# The generated deploy workflow, and the line in it that says which version
# of gojira.yaml the deploy files were rendered from.
CI_WORKFLOW = ".github/workflows/gojira-ci.yaml"
_STAMP = re.compile(r"^\s*GOJIRA_CONTRACT:\s*([0-9a-f]{64})\s*$", re.MULTILINE)

CATCH_UP_TITLE = "kobo: storage and gateway access"
CATCH_UP_BODY = (
    "The room now gives an agent that runs its team's own code the same "
    "things an agent it builds gets. This adds them to this agent. Nothing "
    "already in gojira.yaml is changed or removed.\n\n"
    "Files changed:\n"
    "{room_wiring}"
    "After this merges the room asks the deploy platform to regenerate the "
    "deploy files, which arrives as a second pull request. The agent "
    "restarts when that one is merged.\n\n"
    "gojira.yaml is rewritten from its parsed form, so comments and key "
    "order in that one file are not preserved.\n"
)
CATCH_UP_ALREADY_OPEN = (
    "a pull request with this agent's wiring is already open. Merge or "
    "close it: "
)

# What the room's own wiring branch may touch before the room will recycle it.
_WIRE_FILES = frozenset({
    GOJIRA_YAML,
    "k8s/base/serviceaccount.yaml",
    "k8s/base/kustomization.yaml",
    f"k8s/base/{CONNECTED_STORAGE_FILE}",
})

# The three things the Access tab says about a connected agent's gateway
# identity. `owed` is the short time between a first grant and the request.
WIRED = "wired"
WAITING = "waiting"
OWED = "owed"


def contract_fingerprint(gojira_yaml_text: str) -> str:
    """The deploy platform's own hash of what gojira.yaml MEANS (parsed,
    dumped as sorted JSON), which its build recomputes and compares with the
    one stamped into the workflow. Kept byte-for-byte the same as theirs."""
    doc = yaml.safe_load(gojira_yaml_text)
    canon = json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


def stale_contract(github, repo: str) -> str | None:
    """The fingerprint of the default branch's gojira.yaml when the deploy
    files were rendered from a different one; None when they agree, or when
    either file cannot be read (no claim is made about what was not seen)."""
    text = github.read_file(repo, GOJIRA_YAML)
    workflow = github.read_file(repo, CI_WORKFLOW)
    if not text or not workflow:
        return None
    stamp = _STAMP.search(workflow)
    if stamp is None:
        return None
    try:
        actual = contract_fingerprint(text)
    except yaml.YAMLError:
        return None
    return actual if actual != stamp.group(1) else None


def follow_through(github, gojira, *, repo: str, gojira_app_id: int | None, asked: dict[str, str]) -> bool:
    """Ask the platform to regenerate when `repo`'s deploy files are behind
    its gojira.yaml. Once per version of the file: `asked` remembers the
    fingerprint last asked for, since the platform's own pull request can
    wait on the team for days and asking again changes nothing."""
    if gojira_app_id is None:
        return False
    stale = stale_contract(github, repo)
    if stale is None or asked.get(repo) == stale:
        return False
    gojira.reprovision(gojira_app_id)
    asked[repo] = stale
    log.info("deploy files of %s were behind its gojira.yaml; asked the platform to regenerate", repo)
    return True


def identity_wired(data: dict, agent_name: str) -> bool:
    env = data.get("env")
    env = env if isinstance(env, dict) else {}
    return (
        f"{agent_name}-cabal" in (data.get("secrets_from") or [])
        and bool(env.get(CONNECTED_SWITCHBOARD_URL_ENV))
        and bool(env.get(CONNECTED_GATEWAY_URL_ENV))
    )


def state(github, agent, config) -> dict | None:
    """What the Access tab says about a connected agent's gateway identity,
    or None when the room has no gateway to give one for."""
    if not gateway_identity_on(config):
        return None
    repo = agent.repo_full_name
    data = parse_gojira_yaml(github.read_file(repo, GOJIRA_YAML) or "")
    if identity_wired(data, agent.name):
        return {"state": WIRED, "pull_request": None}
    open_pr = github.find_pr(repo, branch=CONNECTED_WIRE_BRANCH)
    return {"state": WAITING if open_pr else OWED, "pull_request": open_pr}


def offer(session, clients, config, agent) -> str | None:
    """Propose what `add_room_wiring` would add to this agent's repository,
    as one pull request, and return its link; None when the file already
    carries all of it. A request already open on the branch is the answer,
    whatever it carries: the team has something from the room to look at.

    The Secrets are delivered before the request is opened, so they exist
    by the time anything can merge it, and only then: delivering takes back
    the pass before it."""
    github = clients.github
    repo = agent.repo_full_name
    open_pr = github.find_pr(repo, branch=CONNECTED_WIRE_BRANCH)
    if open_pr is not None:
        return open_pr
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        path = workdir / GOJIRA_YAML
        if not path.exists():
            return None
        data = parse_gojira_yaml(path.read_text())
        before = copy.deepcopy(data)
        add_room_wiring(data, agent, config)
        if data == before:
            return None
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        write_connected_storage(workdir, agent)
        deliver_connected_identity(session, clients, config, agent)
        edit = _propose(
            github, workdir, repo,
            branch=CONNECTED_WIRE_BRANCH,
            title=CATCH_UP_TITLE,
            body=CATCH_UP_BODY.replace("{room_wiring}", connected_room_wiring_lines(config)),
            carries=None,
            already_open=CATCH_UP_ALREADY_OPEN,
            files=_WIRE_FILES,
        )
    return edit.pull_request


# --- a room-made Slack app (issue #484) ----------------------------------------
#
# For an agent the room builds, the app's wiring is a commit. Here it is one
# pull request on its own branch: the Secret reference and the wake mode,
# nothing else, since there is no bundle and no runtime pin. The Secret
# itself is delivered before the request opens, so it exists by the time
# anything can merge it.

SLACK_BRANCH = "kobo/slack"
SLACK_PR_TITLE = "kobo: Slack app made by the room"
SLACK_PR_BODY = (
    "The room made a Slack app for this agent. This lets the agent read the "
    "app's values and says how Slack wakes it.\n\n"
    "Files changed:\n"
    "- gojira.yaml: `secrets_from` gains `{agent}-slack` (`SLACK_BOT_TOKEN` "
    "once the app is installed, `SLACK_RELAY_SECRET`, `SLACK_SIGNING_SECRET`, "
    "`SLACK_ICON_URL` and the app's ids), and `SLACK_WAKE_MODE` is set, with "
    "`SLACK_WAKE_EMOJI` when its owner named emoji that wake it and "
    "`SLACK_COMMAND_PUBLIC` when its owner lets its slash command be answered "
    "in public channels it is not in.\n\n"
    "The room hands Slack's events to the agent at `POST /slack/events` with "
    "the header `X-Kobo-Relay-Secret`. The guide section \"Running your own "
    "code\" has the whole contract.\n\n"
    "After this merges the room asks the deploy platform to regenerate the "
    "deploy files, which arrives as a second pull request. The agent "
    "restarts when that one is merged.\n\n"
    "gojira.yaml is rewritten from its parsed form, so comments and key "
    "order in that one file are not preserved.\n"
)
SLACK_PR_ALREADY_OPEN = (
    "a pull request for this agent's Slack app is already open. Merge or "
    "close it: "
)


def slack_wired(data: dict, agent_name: str) -> bool:
    return f"{agent_name}-slack" in (data.get("secrets_from") or [])


def _emoji_in(data: dict) -> str:
    env = data.get("env")
    return str(env.get(WAKE_EMOJI_ENV) or "") if isinstance(env, dict) else ""


def _public_in(data: dict) -> bool:
    env = data.get("env")
    return isinstance(env, dict) and str(env.get(COMMAND_PUBLIC_ENV) or "").lower() == COMMAND_PUBLIC_ON


def add_slack_wiring(
    data: dict, agent_name: str, mode: str, emoji: str | None = None, public: bool | None = None
) -> bool:
    """The Secret reference and the wake mode into a parsed gojira.yaml, and
    the emoji that wake the agent when `emoji` is given ("" takes the key
    out; None leaves it as it is). `public` is whether its slash command is
    answered in public channels it is not in (False takes the key out; None
    leaves it as it is). True when it changed."""
    changed = False
    secrets = data.get("secrets_from")
    if not isinstance(secrets, list):
        secrets = []
        data["secrets_from"] = secrets
    if f"{agent_name}-slack" not in secrets:
        secrets.append(f"{agent_name}-slack")
        changed = True
    env = data.get("env")
    if not isinstance(env, dict):
        env = {}
        data["env"] = env
    if env.get(WAKE_ENV) != mode:
        env[WAKE_ENV] = mode
        changed = True
    if emoji is not None and _emoji_in(data) != emoji:
        if emoji:
            env[WAKE_EMOJI_ENV] = emoji
        else:
            env.pop(WAKE_EMOJI_ENV, None)
        changed = True
    if public is not None and _public_in(data) != public:
        if public:
            env[COMMAND_PUBLIC_ENV] = COMMAND_PUBLIC_ON
        else:
            env.pop(COMMAND_PUBLIC_ENV, None)
        changed = True
    return changed


def propose_slack(
    github, *, repo: str, agent_name: str, mode: str, emoji: str | None = None, public: bool | None = None
) -> RepoEdit:
    """One pull request carrying the Slack wiring with `mode` (and `emoji`
    and `public`, as `add_slack_wiring` reads them), or the one already open that carries
    exactly that; `unchanged` when the default branch already does. Raises
    `RepoBusy` when a different request from the room is open on the branch,
    which includes the case where the default branch already reads this way:
    a request opened for a choice since taken back would otherwise sit open,
    and merging it would undo what the room shows."""
    def carries(data: dict) -> bool:
        env = data.get("env")
        return (
            slack_wired(data, agent_name)
            and isinstance(env, dict)
            and env.get(WAKE_ENV) == mode
            and (emoji is None or _emoji_in(data) == emoji)
            and (public is None or _public_in(data) == public)
        )

    result = edit_gojira_yaml(
        github,
        None,
        repo=repo,
        connected=True,
        gojira_app_id=None,
        edit=lambda data: add_slack_wiring(data, agent_name, mode, emoji, public),
        message=SLACK_PR_TITLE,
        branch=SLACK_BRANCH,
        title=SLACK_PR_TITLE,
        body=SLACK_PR_BODY.replace("{agent}", agent_name),
        carries=carries,
        already_open=SLACK_PR_ALREADY_OPEN,
    )
    if result.how == UNCHANGED:
        open_pr = github.find_pr(repo, branch=SLACK_BRANCH)
        if open_pr is not None and not carries(
            parse_gojira_yaml(github.read_file(repo, GOJIRA_YAML, ref=SLACK_BRANCH) or "")
        ):
            raise RepoBusy(f"{SLACK_PR_ALREADY_OPEN}{open_pr}")
    return result


def slack_state(github, agent) -> dict:
    """Where a connected agent's room-made Slack app stands in its
    repository: `wired` (the default branch reads the Secret and wakes the
    way the room's row says), `waiting` (the room's request is open) or
    `owed` (neither: the request was closed, or never opened)."""
    repo = agent.repo_full_name
    data = parse_gojira_yaml(github.read_file(repo, GOJIRA_YAML) or "")
    env = data.get("env")
    if (
        slack_wired(data, agent.name)
        and isinstance(env, dict)
        and env.get(WAKE_ENV) == effective_wake(agent)
        and _emoji_in(data) == (agent.slack_wake_emoji or "")
        and _public_in(data) == command_public(agent)
    ):
        return {"state": WIRED, "pull_request": None}
    open_pr = github.find_pr(repo, branch=SLACK_BRANCH)
    return {"state": WAITING if open_pr else OWED, "pull_request": open_pr}


def keep(app) -> None:
    """One pass over every ready connected agent: follow a merged request
    through, and offer the room's wiring to one that now needs it. Best
    effort, one agent at a time; nothing here is allowed to stop the rest."""
    from .bab import ROOM_ACTOR
    from .gateway_lane import held_lanes

    clients = app.state.clients
    config = app.state.config
    github = getattr(clients, "github", None)
    gojira = getattr(clients, "gojira", None)
    bab = getattr(clients, "bab", None)
    if github is None:
        return
    asked: dict[str, str] = getattr(app.state, "contract_asked", None) or {}
    settled: set[str] = getattr(app.state, "wiring_settled", None) or set()
    app.state.contract_asked, app.state.wiring_settled = asked, settled
    with app.state.session_factory() as session:
        rows = [
            (a.name, a.repo_full_name, a.gojira_app_id)
            for a in session.query(Agent).filter(Agent.status == AgentStatus.READY, Agent.kind == KIND_CONNECTED).all()
        ]
    for name, repo, app_id in rows:
        if gojira is not None:
            try:
                follow_through(github, gojira, repo=repo, gojira_app_id=app_id, asked=asked)
            except ClientError as exc:
                log.warning("could not follow %s's merged wiring through: %s", name, exc)
        if name in settled or bab is None or not gateway_identity_on(config):
            continue
        try:
            if not held_lanes(bab, name, ROOM_ACTOR):
                continue
            with app.state.session_factory() as session:
                agent = session.query(Agent).filter_by(name=name).one()
                url = offer(session, clients, config, agent)
                session.commit()
        except RepoBusy as exc:
            # Something a team can clear is in the way; said once, not every pass.
            log.warning("wiring for %s is waiting on its repository: %s", name, exc)
            settled.add(name)
            continue
        except ClientError as exc:
            log.warning("wiring for %s could not be offered: %s", name, exc)
            continue
        settled.add(name)
        if url:
            log.info("offered %s the room's wiring: %s", name, url)
