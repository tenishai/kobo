"""Keeping an agent that runs its team's own code wired, after it is connected.

The wire step gives a connected agent everything the room has for it, once,
at connect. Two things happen later and in somebody else's repository, so
no step can do them:

Follow-through. Every change the room makes to a connected agent is a
pull request on its `gojira.yaml`, which the room merges itself (owner
ruling 2026-11-16: connecting a repository hands its deploy files to the
room and the deploy platform). Once one is merged, the file on the default
branch no longer matches the deploy files that were rendered from it, and
the repository's own build refuses to deploy until the platform regenerates
them. The save that merged it asks the platform to; `follow_through` asks
for every repository found behind anyway, once per version of the file: a
request GitHub would not let the room merge is merged later by a person,
and a platform that was down when the save asked still owes the answer
(found 2026-11-13, before it bit). Every ask is a hand-over, so the
platform merges its own pull request too.

Catch-up. An agent connected before the room gave connected agents storage
and a gateway identity has neither. It is offered both, as one pull
request, the first time it holds a permission that needs the gateway: the
moment its team has asked for something that cannot work without it. No
button; an agent that holds nothing is left alone.

Both run from the worker's five-minute pass (`keep`), beside the gateway
heal. Neither pushes to a team's branch: what the room writes there is a
pull request it merges.

Slack. A connected agent that serves a port can have a Slack app the room
makes (issue #484). The app's values and how Slack wakes the agent go to the
Secret `<agent>-slack`; what the agent's repository owes is one reference to
that Secret, which its connect wrote (2026-11-14), so the app and every
later change reach it by a restart. A repository that lacks the reference,
or still sets a wake setting itself that the room has since changed, gets
it as one pull request on `kobo/slack` that the room merges
(`propose_slack`). `slack_state` is what the Slack row reads: wired,
waiting on a merge the room could not make, or owed because the request
was closed.
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
    ROOM_MERGES,
    UNCHANGED,
    RepoBusy,
    RepoEdit,
    _propose,
    edit_gojira_yaml,
    merge_ours,
    parse_gojira_yaml,
)
from kobo_api.clients import ClientError
from kobo_api.kinds import KIND_CONNECTED
from kobo_api.models import Agent, AgentStatus
from kobo_api.slack_setup import (
    BOT_MENTIONS_ENV,
    BOT_MENTIONS_KNOWLEDGE,
    COMMAND_PUBLIC_ENV,
    COMMAND_PUBLIC_ON,
    WAKE_EMOJI_ENV,
    WAKE_ENV,
    wake_settings_of,
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
    name_slack_secret,
    reads_slack_secret,
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
    "{room_wiring}" + ROOM_MERGES +
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
    its gojira.yaml, as a hand-over, so it merges its own pull request.
    Once per version of the file: `asked` remembers the fingerprint last
    asked for, since a pull request GitHub will not let the platform merge
    can wait on a person for days and asking again changes nothing."""
    if gojira_app_id is None:
        return False
    stale = stale_contract(github, repo)
    if stale is None or asked.get(repo) == stale:
        return False
    gojira.reprovision(gojira_app_id, handed_over=True)
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


def offer(session, clients, config, agent) -> RepoEdit | None:
    """Add what `add_room_wiring` would add to this agent's repository, as
    one pull request the room merges: `committed` once it is in,
    `pull_request` with its link when GitHub would not let the room merge
    it, None when the file already carries all of it. A request already
    open on the branch is the answer, whatever it carries, and is merged
    when it is still only the room's own work.

    The Secrets are delivered before the request is opened, so they exist
    by the time anything can merge it, and only then: delivering takes back
    the pass before it."""
    github = clients.github
    repo = agent.repo_full_name
    open_pr = github.find_pr(repo, branch=CONNECTED_WIRE_BRANCH)
    if open_pr is not None:
        return merge_ours(github, repo, open_pr, branch=CONNECTED_WIRE_BRANCH, files=_WIRE_FILES)
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
        return _propose(
            github, workdir, repo,
            branch=CONNECTED_WIRE_BRANCH,
            title=CATCH_UP_TITLE,
            body=CATCH_UP_BODY.replace("{room_wiring}", connected_room_wiring_lines(config)),
            carries=None,
            already_open=CATCH_UP_ALREADY_OPEN,
            files=_WIRE_FILES,
        )


# --- a room-made Slack app (issue #484) ----------------------------------------
#
# For an agent the room builds, the app's wiring is a commit. Here the room
# does not push, so everything the app needs travels in the Secret
# `<agent>-slack`, how Slack wakes the agent included, and the repository
# owes one line: that it reads the Secret when it exists. An agent connected
# since 2026-11-14 has that line from its connect (`steps.name_slack_secret`),
# so making its app and changing how it wakes are a delivery and a restart.
# One connected before gets the line as a single pull request on its own
# branch, which the room merges: the last one Slack needs there.

SLACK_BRANCH = "kobo/slack"
SLACK_PR_TITLE = "kobo: Slack app made by the room"
SLACK_PR_BODY = (
    "The room made a Slack app for this agent, and keeps the app's values "
    "and how Slack wakes the agent in the Secret `{agent}-slack`. This makes "
    "the agent read all of it from there.\n\n"
    "Files changed:\n"
    "- gojira.yaml: `secrets_from_optional` gains `{agent}-slack`, unless "
    "the file already reads that Secret. It carries `SLACK_BOT_TOKEN` once "
    "the app is installed, `SLACK_RELAY_SECRET`, `SLACK_SIGNING_SECRET`, "
    "`SLACK_ICON_URL`, the app's ids, `SLACK_WAKE_MODE`, `SLACK_WAKE_EMOJI` "
    "when its owner named emoji that wake it, `SLACK_COMMAND_PUBLIC` "
    "when its owner lets its slash command be answered in public channels "
    "it is not in, and `SLACK_BOT_MENTIONS` when its owner changed what "
    "another app's mention may do.\n"
    "- gojira.yaml: `SLACK_WAKE_MODE`, `SLACK_WAKE_EMOJI`, "
    "`SLACK_COMMAND_PUBLIC` and `SLACK_BOT_MENTIONS` are taken out of `env` "
    "where they are set. A "
    "value there hides the one in the Secret, so the agent would keep "
    "waking the old way.\n\n"
    "This is the last pull request Slack needs in this repository: from here "
    "a change to how Slack wakes the agent is delivered to the Secret and "
    "the agent is restarted.\n\n"
    "The room hands Slack's events to the agent at `POST /slack/events` with "
    "the header `X-Kobo-Relay-Secret`. The guide section \"Running your own "
    "code\" has the whole contract.\n\n" + ROOM_MERGES +
    "gojira.yaml is rewritten from its parsed form, so comments and key "
    "order in that one file are not preserved.\n"
)
SLACK_PR_ALREADY_OPEN = (
    "a pull request for this agent's Slack app is already open. Merge or "
    "close it: "
)

_WAKE_KEYS = (WAKE_ENV, WAKE_EMOJI_ENV, COMMAND_PUBLIC_ENV, BOT_MENTIONS_ENV)


def _set_in_file(data: dict) -> dict[str, str]:
    """The wake settings a gojira.yaml sets itself. Each hides the one the
    room delivers, which is how every connected agent was wired until
    2026-11-14 and how a team may still choose to hold one."""
    env = data.get("env")
    if not isinstance(env, dict):
        return {}
    return {key: str(env[key] or "") for key in _WAKE_KEYS if key in env}


def _same(key: str, in_file: str, wanted: str | None) -> bool:
    if key == COMMAND_PUBLIC_ENV:
        return (in_file.lower() == COMMAND_PUBLIC_ON) == (wanted == COMMAND_PUBLIC_ON)
    if key == BOT_MENTIONS_ENV:
        # Not set and `knowledge` are the same thing to the runtime.
        return (in_file.lower() or BOT_MENTIONS_KNOWLEDGE) == (wanted or BOT_MENTIONS_KNOWLEDGE)
    return in_file == (wanted or "")


def follows_room(data: dict, agent_name: str, settings: dict[str, str | None]) -> bool:
    """Whether an agent started from this gojira.yaml wakes the way
    `settings` say once they are delivered: it reads the Secret, and
    whatever the file still sets itself says the same."""
    return reads_slack_secret(data, agent_name) and all(
        _same(key, value, settings.get(key)) for key, value in _set_in_file(data).items()
    )


def slack_follows_room(github, *, repo: str, agent_name: str, settings: dict[str, str | None]) -> bool:
    """`follows_room` for the default branch: when true, a delivery and a
    restart are the whole change and the team is asked for nothing."""
    return follows_room(parse_gojira_yaml(github.read_file(repo, GOJIRA_YAML) or ""), agent_name, settings)


def hand_slack_to_room(data: dict, agent_name: str) -> bool:
    """The Secret reference into a parsed gojira.yaml that lacks it, and the
    wake settings out of its `env`. True when it changed."""
    changed = name_slack_secret(data, agent_name)
    env = data.get("env")
    for key in _set_in_file(data):
        del env[key]
        changed = True
    return changed


def propose_slack(github, *, repo: str, agent_name: str, gojira=None, gojira_app_id: int | None = None) -> RepoEdit:
    """One pull request that leaves the agent reading `<agent>-slack` and
    setting none of the wake settings itself, or the one already open that
    does exactly that, merged by the room (`committed`; `pull_request` with
    its link when GitHub would not let it); `unchanged` when the default
    branch already does. With `gojira` and the agent's app id, a merged one
    is followed through at once instead of at the next pass.
    Raises `RepoBusy` when a different request from the room is open on the
    branch, which includes the case where the default branch already reads
    this way: merging what is open would hide what the room delivers."""
    def carries(data: dict) -> bool:
        return reads_slack_secret(data, agent_name) and not _set_in_file(data)

    result = edit_gojira_yaml(
        github,
        gojira,
        repo=repo,
        connected=True,
        gojira_app_id=gojira_app_id,
        edit=lambda data: hand_slack_to_room(data, agent_name),
        message=SLACK_PR_TITLE,
        branch=SLACK_BRANCH,
        title=SLACK_PR_TITLE,
        body=SLACK_PR_BODY.replace("{agent}", agent_name),
        carries=carries,
        already_open=SLACK_PR_ALREADY_OPEN,
    )
    if result.how == UNCHANGED:
        refuse_other_slack_request(github, repo=repo, agent_name=agent_name)
    return result


def refuse_other_slack_request(github, *, repo: str, agent_name: str) -> None:
    """Raise `RepoBusy` when a request is open on the room's Slack branch
    that would leave the file setting how the agent wakes: for a repository
    whose default branch needs no request, where merging that one later
    would hide what the room delivers. One that hands the settings to the
    room is left to be merged."""
    open_pr = github.find_pr(repo, branch=SLACK_BRANCH)
    if open_pr is None:
        return
    data = parse_gojira_yaml(github.read_file(repo, GOJIRA_YAML, ref=SLACK_BRANCH) or "")
    if not (reads_slack_secret(data, agent_name) and not _set_in_file(data)):
        raise RepoBusy(f"{SLACK_PR_ALREADY_OPEN}{open_pr}")


def slack_state(github, agent) -> dict:
    """Where a connected agent's room-made Slack app stands in its
    repository: `wired` (an agent started from the default branch wakes the
    way the room's row says), `waiting` (the room's request is open) or
    `owed` (neither: the request was closed, or never opened)."""
    repo = agent.repo_full_name
    if slack_follows_room(github, repo=repo, agent_name=agent.name, settings=wake_settings_of(agent)):
        return {"state": WIRED, "pull_request": None}
    open_pr = github.find_pr(repo, branch=SLACK_BRANCH)
    return {"state": WAITING if open_pr else OWED, "pull_request": open_pr}


def keep(app) -> None:
    """One pass over every ready connected agent: give the room's wiring to
    one that now needs it, and follow a merged request through (the one
    this pass merged included). Best effort, one agent at a time; nothing
    here is allowed to stop the rest."""
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
        if name not in settled and bab is not None and gateway_identity_on(config):
            _offer_once(app, name, settled)
        if gojira is not None:
            try:
                follow_through(github, gojira, repo=repo, gojira_app_id=app_id, asked=asked)
            except ClientError as exc:
                log.warning("could not follow %s's merged wiring through: %s", name, exc)


def _offer_once(app, name: str, settled: set[str]) -> None:
    """`offer` for one agent that holds a permission, remembered in
    `settled` once it has an answer a later pass would only repeat."""
    from .bab import ROOM_ACTOR
    from .gateway_lane import current, held_permissions, lanes_of

    clients = app.state.clients
    try:
        with app.state.session_factory() as session:
            # A permission that needs the gateway: one with a lane. Before the room has read the
            # gateway's cards it cannot tell which do, and any permission it holds counts.
            held, lanes = held_permissions(clients.bab, name, ROOM_ACTOR), current(session, clients)
            if not held or (lanes.known and not lanes_of(held, lanes)):
                return
            agent = session.query(Agent).filter_by(name=name).one()
            edit = offer(session, clients, app.state.config, agent)
            session.commit()
    except RepoBusy as exc:
        # Something a person can clear is in the way; said once, not every pass.
        log.warning("wiring for %s is waiting on its repository: %s", name, exc)
        settled.add(name)
        return
    except ClientError as exc:
        log.warning("wiring for %s could not be added: %s", name, exc)
        return
    settled.add(name)
    if edit is not None:
        log.info("added the room's wiring to %s: %s", name, edit.pull_request or "merged")
