"""The rules behind the Secrets and environment panel: which key names an
owner may use, the two edits their writes make to `gojira.yaml`, and the
one flow that gets those edits into a repository.

The first half is pure functions over plain dicts, deliberately. The
routes in `kobo_api.main` hold the guards and the lane client holds the
network; what is left is a set of decisions worth stating once and testing
directly, because all four surfaces (both routes, both MCP tools) have to
make them identically.

The second half (`edit_gojira_yaml`) is the clone-edit-push flow, modelled
on `kobo_api.slack_setup.wire_slack` and `kobo_api.steps.
ConnectedWireStep` rather than on either of them alone, because this is
the first write in the room that has to be BOTH: a composed agent's repo
is the room's to commit to, and a connected agent's is not, so the same
edit becomes a pull request there.

Two lists, one rule set (design 2026-09-22). A secret and an environment
variable differ in where they are held and who can read them back, not in
what a key may be called: both arrive in the pod as environment variables,
so a name either works there or does not.

One thing here is a SECOND wall rather than a first. `write_refusal`
refuses a key the room manages, and `apply_env` refuses to write one
anyway -- so a caller who found a way past a route could still not point
an agent's model access somewhere else by writing `VERTEXAI_LOCATION`
through the environment list.

No value is ever read, logged, compared or returned by anything in this
module except `apply_env`, which puts one into a mapping the caller is
about to write to a repository file. That is the environment list, which
is for values that are not sensitive; the secrets list never reaches this
module at all, only its key names.
"""

from __future__ import annotations

import logging
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from kobo_api.clients import (
    COMMIT_AUTHOR_EMAIL,
    ClientError,
    GitHubClient,
    GojiraClient,
)

log = logging.getLogger(__name__)

# The one name rule, enforced identically on both lists. An environment
# variable name in a shell is conventionally upper snake case, and the
# deploy platform renders these straight into a Deployment, so anything a
# shell would not name is refused here rather than at the cluster.
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Long enough for any real name, short enough that a pasted blob of prose
# is refused as a name rather than stored as one.
MAX_KEY_LENGTH = 64

# Per list, and counted against what the write LEAVES BEHIND rather than
# what it adds -- swapping one key for another at the cap is not growth.
MAX_KEYS = 100

# One value's ceiling. Generous for a key, a URL or a PEM block, and low
# enough that a whole pasted file cannot ride in as one variable. Enforced
# at the request model so an oversized body is refused before it reaches a
# repository or the lane; the number is here so both surfaces read it from
# the same place as the rest of the rules.
MAX_ENV_VALUE_LENGTH = 8192

# What the room says about a key it sets itself. Named once because the
# refusal below and the panel's own inline copy both use it.
ROOM_SETS_THIS_ONE = "the room sets this one"

# And what it says about a key the CONTAINER sets. A different sentence
# because it is a different fact: nobody in this room decided these, the
# image and the runtime did, and an owner who overwrote one would not get
# a custom variable, they would get an agent that cannot find its own
# interpreter or its own binaries.
CONTAINER_SETS_THIS_ONE = "the container sets this one"

# The two direct-Vertex variables `kobo_api.steps._set_service_account_and_
# model_env` writes into an agent's `gojira.yaml` only when the room has no
# model gateway (with the gateway on an agent's model access is its
# `MODELS_GATEWAY_KEY` Secret, and these two are not written; a stale copy
# is removed on reprovision). Reserved either way: an owner may not take
# them. Spelled out here rather than imported from that module: this is a
# decision about the panel, and importing would put a cycle between this
# module and the step engine that reaches it.
MODEL_ENV_KEYS = ("VERTEXAI_PROJECT", "VERTEXAI_LOCATION")

# Every key the room fills in on an agent's behalf: the colleague pass
# (`kobo_api.composer.CABAL_COLLEAGUE_TOKEN_ENV`), the Slack tokens
# (`kobo_api.slack_setup.wire_slack`), and the two above. An owner who
# writes one of these does not get a second copy of it, they get their
# agent's own wiring overwritten, so all of them are refused by name.
RESERVED_KEYS = frozenset(
    {
        "CABAL_COLLEAGUE_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_SIGNING_SECRET",
        "SLACK_ICON_URL",
        # The model the room picked for the agent (PATCH /api/agents/{name}/model):
        # the composed agent.yaml reads `${AGENT_MODEL:-<birth model>}`.
        "AGENT_MODEL",
        # How Slack wakes the agent; set from the Slack tab, written by the room.
        "SLACK_WAKE_MODE",
        # The agent's key for the room's model gateway (kobo#390), minted and
        # delivered by the room.
        "MODELS_GATEWAY_KEY",
        *MODEL_ENV_KEYS,
        # What the room tells an agent that runs its team's own code
        # (`kobo_api.steps.add_room_wiring`): where its storage is, and
        # where the gateways are.
        "KOBO_DATA_DIR",
        "KOBO_SWITCHBOARD_URL",
        "KOBO_GATEWAY_URL",
        "KOBO_MODELS_URL",
    }
)

# The Slack names are the room's only where the room made the Slack app. An
# agent that runs its team's own code and brings its own app reads its
# tokens under these very names, so its Secrets tab has to take them
# (2026-11-13: the row told a team to save its tokens under Secrets and the
# save refused the two names every Slack library reads). Released by the
# secrets route for a connected agent with no room-made app, and held again
# once it has one, when `<agent>-slack` carries them.
TEAM_SLACK_KEYS = frozenset({"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_SIGNING_SECRET"})

# Names the RUNTIME owns, on both lists. Not the room's to set and not an
# owner's either: each of these decides how the container finds something
# it cannot run without.
#
# `PATH` and `HOME` are how a process finds its binaries and its own
# directory. `PYTHONPATH` and `PYTHONHOME` decide which interpreter and
# which libraries the agent imports, and a wrong one is a container that
# does not boot. `LD_PRELOAD` and `LD_LIBRARY_PATH` decide which shared
# objects load into every process in the container before its own code
# runs, which is not a setting so much as a way to run arbitrary code
# inside the pod; nothing an owner needs is on the other side of it.
CONTAINER_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "PYTHONPATH",
        "PYTHONHOME",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
    }
)


def secret_path(agent_name: str) -> str:
    """Where this agent's own secrets are held on the secrets platform.

    The third path under the agent, beside the two the room already
    writes (`apps/<agent>/cabal`, `apps/<agent>/slack`).
    """
    return f"apps/{agent_name}/env"


def env_secret_name(agent_name: str) -> str:
    """The Kubernetes Secret the platform delivers `secret_path` into.

    Same naming pattern as `<agent>-cabal` and `<agent>-slack`, which is
    what lets the deploy platform turn it into `envFrom` with nothing new
    asked of it: one more entry in `secrets_from`.
    """
    return f"{agent_name}-env"


# The two secrets the ROOM fills in for an agent, by the purpose each is
# stored under (`apps/<agent>/cabal`, `apps/<agent>/slack`). Listed here
# so the panel can show them as locked rows rather than leaving an owner
# to wonder where their agent's colleague pass went: `RESERVED_KEYS`
# already refuses these names, and a list that refuses a name it does not
# show is a list that looks broken.
MANAGED_PURPOSES = ("cabal", "slack")


def managed_secret_path(agent_name: str, purpose: str) -> str:
    """Where the room's own secret for `purpose` is held. The two siblings
    of `secret_path`, named the same way for the same reason."""
    return f"apps/{agent_name}/{purpose}"


def managed_secret_name(agent_name: str, purpose: str) -> str:
    """The Kubernetes Secret the platform delivers `managed_secret_path`
    into: `<agent>-cabal`, `<agent>-slack`."""
    return f"{agent_name}-{purpose}"


# --- Is a stored secret actually ON the agent? --------------------------------
#
# A name in a list is not a credential in a pod. Between the two sits the
# platform's own ExternalSecret, and the only honest way to say "this is
# set" is to read that object's Ready condition -- never the Kubernetes
# Secret it owns, which holds the values themselves and which nothing in
# this room reads.

LIVE = "live"
SYNCING = "syncing"
SYNC_ERROR = "error"
UNWIRED = "unwired"

# What the room says about each state, in its own words. The platform's
# own condition MESSAGE is never one of these: it is a sentence somebody
# else wrote about a request the room made, and a provider that echoed
# what it was asked to store would put a value in this room's UI. Only the
# condition's REASON survives, and only when it is one plain word.
SYNC_NOT_DELIVERED = "The room has not delivered this one to the agent yet."
SYNC_FAILED = "The secrets platform could not deliver this one to the agent."
SYNC_UNKNOWN = "The room could not check with the cluster just now."

# A Kubernetes condition reason is a single CamelCase word by convention.
# Anything else is not repeated at all: this is the one field of somebody
# else's object that reaches a person's screen, so it has to prove it is a
# word before it gets there.
_REASON_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")


def _ready_condition(status: dict) -> dict | None:
    """The `Ready` entry of an ExternalSecret's status conditions, or None
    when it has not got one yet."""
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return None
    for condition in conditions:
        if isinstance(condition, dict) and condition.get("type") == "Ready":
            return condition
    return None


def sync_state(status: dict | None) -> dict[str, str | None]:
    """What one ExternalSecret's status says, as the panel's lock.

    `status` is the object's own `.status` mapping, or None when there is
    no such object in the namespace at all. Four answers, and the shape is
    the same for the agent's own secrets and for the two the room sets:

    - `live`: the Ready condition is True. The value is in the pod's
      Secret, which is the only claim this panel is allowed to make.
    - `error`: Ready is False. The reason word comes along when it is a
      word; the platform's own message never does.
    - `syncing`: the object exists and has not said Ready either way yet,
      which is the ordinary state for the first seconds after a save.
    - `unwired`: there is no object, so nothing is delivering this.
    """
    if status is None:
        return {"state": UNWIRED, "message": SYNC_NOT_DELIVERED}

    condition = _ready_condition(status)
    if condition is None:
        return {"state": SYNCING, "message": None}

    ready = str(condition.get("status", "")).lower()
    if ready == "true":
        return {"state": LIVE, "message": None}
    if ready != "false":
        return {"state": SYNCING, "message": None}

    reason = condition.get("reason")
    if isinstance(reason, str) and _REASON_RE.match(reason):
        return {"state": SYNC_ERROR, "message": f"{SYNC_FAILED} It reported {reason}."}
    return {"state": SYNC_ERROR, "message": SYNC_FAILED}


def sync_unknown() -> dict[str, str | None]:
    """What the panel shows when the room could not ask the cluster at all.

    Deliberately `syncing` rather than `error`: the panel's error tag says
    the secret is not on the agent, and a failed LOOK is not evidence of
    that. "We have not got a settled answer" is the honest reading of a
    read that did not happen, and the message says which it was.
    """
    return {"state": SYNCING, "message": SYNC_UNKNOWN}


def key_refusal(key: str, released: frozenset[str] = frozenset()) -> str | None:
    """The plain sentence refusing `key`, or None when it is allowed.
    `released` is the reserved names this agent's team keeps for itself
    (`TEAM_SLACK_KEYS`).

    Checked in the order a person would: is this a name at all, is it a
    reasonable length, is it one the room already fills in, and is it one
    the container owns. The two reserved checks come last so a mistyped
    reserved name reads as a mistyped name, which is what it is, and they
    are separate because they are different facts with different fixes.
    """
    if not KEY_RE.match(key):
        return (
            f"{key or 'that name'}: use capital letters, digits and "
            "underscores, starting with a letter"
        )
    if len(key) > MAX_KEY_LENGTH:
        return f"{key}: keep a name to {MAX_KEY_LENGTH} characters or fewer"
    if key in RESERVED_KEYS and key not in released:
        return f"{key}: {ROOM_SETS_THIS_ONE}"
    if key in CONTAINER_KEYS:
        return f"{key}: {CONTAINER_SETS_THIS_ONE}"
    return None


def write_refusal(
    values: dict[str, str],
    remove: list[str],
    existing: list[str],
    *,
    taken: Iterable[str] = (),
    taken_as: str = "",
    released: frozenset[str] = frozenset(),
) -> str | None:
    """The plain sentence refusing one whole write, or None to go ahead.

    `values` is what the caller is setting, `remove` what they are taking
    away, and `existing` the key names already held -- names only, never
    values, which is all the caller of this ever has for the secrets list
    anyway. `taken` is the names held on the OTHER list (`taken_as` says
    which: "secret" or "variable"), so one name cannot be both at once.

    Four refusals, in the order that answers a person best: a name that is
    not allowed (either side, so a remove is checked exactly as a set is),
    a key named on both sides at once (which of the two was meant is not
    something to guess at), a key the other list already holds (both
    arrive in the pod as one environment variable, and which value wins
    would be whichever was written last; issue #272), and a write that
    would leave more keys behind than a list may hold.
    """
    for key in list(values) + list(remove):
        refusal = key_refusal(key, released)
        if refusal is not None:
            return refusal

    both = sorted(set(values) & set(remove))
    if both:
        return f"{both[0]} is in both the add list and the remove list"

    crossed = sorted(set(values) & set(taken))
    if crossed:
        return f"{crossed[0]} is already a {taken_as} on this agent"

    left = merged_names(existing, values, remove)
    if len(left) > MAX_KEYS:
        return f"that would leave more than {MAX_KEYS} keys; remove some first"
    return None


def merged_names(
    existing: list[str], values: dict[str, str], remove: list[str]
) -> list[str]:
    """The key names a write leaves behind, sorted.

    Set-shaped on purpose: replacing a key that is already there does not
    add one, and removing a key that is not there takes nothing away. Both
    of those are what make a retried save answer the same as the first.
    """
    return sorted((set(existing) | set(values)) - set(remove))


def read_env(data: dict) -> dict[str, str]:
    """The environment variables an OWNER set, out of a parsed
    `gojira.yaml` -- never one the room wires in itself.

    Every `RESERVED_KEYS` name is left out, not only the two model ones.
    Until issue #250 this dropped `MODEL_ENV_KEYS` alone, so an agent
    with Slack wired handed `SLACK_WAKE_MODE` back as an owner row; the
    panel re-sent it on every save and `write_refusal` refused the whole
    write by that name. What the room sets is read by `read_room_env`.

    A non-mapping `env` reads as empty rather than raising: a file shaped
    that way could never have run on the deploy platform, so there is
    nothing in it to show, and a read is not the place to say so.

    Values are rendered as text because YAML gives back an int for
    `PORT: 8080` and a bool for `DEBUG: true`, and what the panel edits is
    a text field. `True` renders as `true`, the way the file spelled it.
    """
    return _env_rows(data, lambda key: key not in RESERVED_KEYS)


def read_room_env(data: dict) -> dict[str, str]:
    """The environment variables the ROOM set on this agent, out of a
    parsed `gojira.yaml`: the `RESERVED_KEYS` names the file carries.

    The complement of `read_env`, so the panel can show them as locked
    rows the way it shows the room's own secrets. Values ride along
    because this list is the non-sensitive one and the file is readable
    by anybody who can read the panel.
    """
    return _env_rows(data, lambda key: key in RESERVED_KEYS)


def _env_rows(data: dict, keep: Callable[[str], bool]) -> dict[str, str]:
    env = data.get("env")
    if not isinstance(env, dict):
        return {}
    return {str(key): _as_text(value) for key, value in env.items() if keep(str(key))}


def apply_env(data: dict, values: dict[str, str], remove: list[str]) -> bool:
    """Set and remove environment variables in a parsed `gojira.yaml`,
    in place. True when the file actually changed.

    The two model keys are neither written nor removed, whatever a caller
    asked for -- see this module's own docstring for why that wall stands
    here as well as at the route.

    A non-mapping `env` is replaced rather than merged into, the same
    decision `kobo_api.steps._set_service_account_and_model_env` already
    makes about a file the platform could not have run.
    """
    before = data.get("env")
    current = dict(before) if isinstance(before, dict) else {}

    for key in remove:
        if key not in MODEL_ENV_KEYS:
            current.pop(key, None)
    for key, value in values.items():
        if key not in MODEL_ENV_KEYS:
            current[key] = value

    changed = not isinstance(before, dict) or {
        str(k): _as_text(v) for k, v in before.items()
    } != {str(k): _as_text(v) for k, v in current.items()}
    data["env"] = current
    return changed


def wire_env_secret(data: dict, agent_name: str) -> bool:
    """Append `<agent>-env` to a parsed `gojira.yaml`'s `secrets_from`,
    in place. True when the entry was not already there.

    The one-time edit the first secret makes. Append-if-missing and never
    replacing what is already in the list -- the same discipline
    `kobo_api.steps._append_secrets_from` and `kobo_api.slack_setup.
    _update_gojira_secrets_from` already hold for the room's other two
    Secrets, and the reason a second save does not open a second pull
    request against somebody's repository.
    """
    entry = env_secret_name(agent_name)
    if _optional_reference(data, entry):
        return False  # referenced at birth as optional (tenishai/kobo#434)
    existing = data.get("secrets_from")
    secrets_from = list(existing) if isinstance(existing, list) else []
    if entry in secrets_from:
        data["secrets_from"] = secrets_from
        return False
    secrets_from.append(entry)
    data["secrets_from"] = secrets_from
    return True


def has_env_secret(data: dict, agent_name: str) -> bool:
    """Whether a parsed `gojira.yaml` already names this agent's own
    secrets Secret in `secrets_from`.

    Asked of the DEFAULT branch to decide whether the wiring line still
    has to be added, and of a proposed branch's head to decide whether an
    open pull request is already carrying it -- one question, so one
    function, because two spellings of it could disagree.
    """
    existing = data.get("secrets_from")
    entry = env_secret_name(agent_name)
    return (isinstance(existing, list) and entry in existing) or _optional_reference(data, entry)


def _optional_reference(data: dict, entry: str) -> bool:
    """Whether gojira.yaml names `entry` among the Secrets the pod reads when they
    exist (`secrets_from_optional`, wired at birth since tenishai/kobo#434): such a
    reference never needs adding or removing, the pod tolerates the Secret missing."""
    optional = data.get("secrets_from_optional")
    return isinstance(optional, list) and entry in optional


def unwire_env_secret(data: dict, agent_name: str) -> bool:
    """Remove `<agent>-env` from a parsed `gojira.yaml`'s `secrets_from`,
    in place. True when it was there.

    The mirror of `wire_env_secret`, and the reason emptying the secrets
    list is allowed rather than refused: an agent with no secrets left
    should not carry a line naming a Kubernetes Secret that no longer
    exists, because the deploy platform turns that line into an `envFrom`
    and a pod whose `envFrom` names a missing Secret does not start.

    Everything else in the list is left exactly where it is: a team's own
    entries on a connected agent's file are none of this function's
    business.
    """
    if _optional_reference(data, env_secret_name(agent_name)):
        return False  # an optional reference stays: the pod boots without the Secret
    existing = data.get("secrets_from")
    if not isinstance(existing, list):
        return False
    entry = env_secret_name(agent_name)
    if entry not in existing:
        return False
    data["secrets_from"] = [name for name in existing if name != entry]
    return True


def _as_text(value: object) -> str:
    """One YAML scalar as the text a form field would hold. Booleans keep
    YAML's own lowercase spelling rather than Python's."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


# --- Getting an edit into the agent's repository ------------------------------

# The two branches the room proposes a connected agent's changes on.
# SEPARATE, and that is the whole reason there are two: the wiring branch
# carries one line that is always the same (`<agent>-env` in
# `secrets_from`) and is safe to leave open across many saves, while the
# environment branch carries whatever a person just typed and is a
# different change every time. Sharing one branch meant a pending
# environment pull request could be mistaken for a wiring one and reported
# as already carrying a line it had never seen.
#
# Fixed names rather than per-save, for the reason `kobo_api.steps.
# CONNECTED_WIRE_BRANCH` gives: a fresh branch per attempt leaves a trail
# of abandoned pull requests in somebody else's repository.
ENV_BRANCH = "kobo/env"
SECRETS_WIRING_BRANCH = "kobo/secrets-wiring"

# The one file either edit writes, and what a branch has to be limited to
# before the room will delete it.
GOJIRA_YAML = "gojira.yaml"

# What a branch is compared AGAINST. `HEAD` resolves to the repository's
# own default branch whatever it is called, so nothing here has to know
# whether a team calls it main, master or something else.
DEFAULT_BRANCH_REF = "HEAD"

ENV_PR_TITLE = "kobo: environment variables"
SECRETS_WIRING_PR_TITLE = "kobo: read the agent's own secrets"

# The sentence both bodies end on, because it is the one thing about
# either change that is not a plain addition and the one thing an owner
# reviewing it cannot see from the diff alone until it is too late.
_REWRITTEN_FROM_PARSED = (
    "gojira.yaml is rewritten from its parsed form, so comments, blank "
    "lines and key order in that one file are NOT preserved. Nothing else "
    "in the repository is touched.\n"
)

ENV_PR_BODY = (
    "This changes the environment variables the agent runs with.\n\n"
    "Files changed:\n"
    "- gojira.yaml: the `env` mapping. These are values that are not "
    "sensitive; secrets are held in the secrets platform and never appear "
    "in this file.\n\n" + _REWRITTEN_FROM_PARSED
)

SECRETS_WIRING_PR_BODY = (
    "This lets the agent read the secrets its owner added in the room.\n\n"
    "Files changed:\n"
    "- gojira.yaml: one entry added to `secrets_from`, naming the "
    "Kubernetes Secret the secrets platform delivers into this agent's "
    "namespace. Secret VALUES are not here and never will be: this file "
    "only names the Secret.\n\n" + _REWRITTEN_FROM_PARSED
)

SECRETS_UNWIRE_PR_TITLE = "kobo: the agent has no secrets of its own"

SECRETS_UNWIRE_PR_BODY = (
    "The agent's owner removed the last secret it had in the room, so it "
    "no longer needs the line that names one.\n\n"
    "Files changed:\n"
    "- gojira.yaml: one entry removed from `secrets_from`.\n\n"
    "Merging this is what finishes the removal. The stored secret is kept "
    "until then, on purpose: taking it away while this file still names it "
    "would stop the agent from starting.\n\n" + _REWRITTEN_FROM_PARSED
)

# What the room says when a pull request it opened before is still open on
# the same branch and this save is not the same change. Pushing the branch
# again would fail non fast-forward, and reusing the open pull request
# would report a change as proposed that is not in it.
#
# One sentence per branch, because the two branches carry different
# changes and "your environment request is open" in front of somebody
# adding a SECRET names the wrong thing to go and merge.
ENV_PR_ALREADY_OPEN = (
    "a pull request with this agent's environment variables is already "
    "open. Merge or close it, then save again: "
)
WIRING_PR_ALREADY_OPEN = (
    "a pull request with this agent's secrets wiring is already open. "
    "Merge or close it, then save again: "
)

# What the room says about a branch that carries its name and not its
# work. Never deleted: the room proves a branch is its own before
# recycling one, and cannot prove this.
def branch_not_ours(branch: str) -> str:
    return (
        f"a branch named {branch} already exists and the room did not make "
        "it. Rename or delete it, then try again."
    )

# How a change reached the agent's repository. `unchanged` means the file
# already said this, which is what a retried save answers.
COMMITTED = "committed"
PULL_REQUEST = "pull_request"
UNCHANGED = "unchanged"

# The two ways `gojira.yaml` can be unreadable rather than absent. Named
# once because both the read and the write answer with it, and because
# neither is something a retry fixes: somebody has to edit the file.
UNREADABLE_GOJIRA_YAML = (
    "this agent's gojira.yaml cannot be read as YAML, so the room cannot "
    "change its environment. Fix that file, then save again."
)
MISSING_GOJIRA_YAML = (
    "this agent's repository has no gojira.yaml, so there is nothing to "
    "set the environment on"
)
# Told apart from "no file" on purpose (there is no such file vs. the room
# may not look): one is the team's to fix and the other is an access
# problem, and answering the first for the second sends somebody hunting
# for a file that is sitting right there.
UNREADABLE_REPOSITORY = (
    "the room cannot read this agent's repository right now, so it cannot "
    "read or change its environment"
)


class RepoBusy(ClientError):
    """Something an OWNER can clear is standing in front of this change:
    a pull request the room opened before, or a file nothing here can
    parse.

    A `ClientError` subclass so every existing `except ClientError` still
    catches it, and its own type so a route can tell "say this sentence to
    the person" from "the platform was unreachable, say the house line".
    Nothing here is retryable by the room; all of it is retryable by a
    person who merges, closes or fixes something.

    A leftover BRANCH is deliberately not one of these any more: a branch
    the room named itself, with no pull request open from it, is the
    room's to recycle (see `_propose`).
    """


class DeployTriggerFailed(ClientError):
    """The repository change landed and the platform would not regenerate.

    Carries `pushed` because the two cases need two different sentences.
    A save that pushed something and then failed here HAS changed the
    agent's files, and a retry will find nothing to push; a save that
    changed nothing and failed here changed nothing at all. Telling
    somebody "couldn't save" for the first would be a lie about their own
    file.
    """

    def __init__(self, message: str, *, pushed: bool) -> None:
        super().__init__(message)
        self.pushed = pushed


@dataclass(frozen=True)
class RepoEdit:
    """What happened to the agent's repository, in the words a route hands
    back to a person: which of the three ways it went, and the pull request
    to follow when there is one."""

    how: str
    pull_request: str | None = None


def parse_gojira_yaml(text: str | None) -> dict:
    """One `gojira.yaml`'s text as a mapping, or `RepoBusy`.

    Empty and absent both read as an empty mapping, because a repository
    that has not been wired yet is not an error. A file that does not
    PARSE is, and so is one that parses to something other than a mapping:
    neither is a state the deploy platform could have run, and writing
    over it would throw away whatever a person meant to put there. Both
    answer the same sentence, which names the file, because "your YAML is
    broken" and "your YAML is a list" are the same job to fix.
    """
    try:
        data = yaml.safe_load(text or "") or {}
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise RepoBusy(UNREADABLE_GOJIRA_YAML) from exc
    if not isinstance(data, dict):
        raise RepoBusy(UNREADABLE_GOJIRA_YAML)
    return data


def read_default_gojira(github: GitHubClient, repo: str) -> dict:
    """`repo`'s `gojira.yaml` on its default branch, parsed.

    Permission is checked FIRST, the way the connect scan does, because
    `GitHubClient.read_file` answers None for a file the room may not read
    exactly as it does for a file that is not there. Those are different
    answers to a person -- one is "add the file", the other is "the room
    lost access to this repository" -- and collapsing them would send
    somebody hunting for a file sitting in front of them. It matters more
    for the WIRING question than for the variables: an unreadable
    repository answering "no variables" is merely unhelpful, but one
    answering "not wired" would have the room propose a line that is
    already there.

    A missing or empty file parses to an empty mapping. An unparseable one
    is `RepoBusy`, not an empty read: showing an empty list over a file
    nobody here could parse invites a save that would overwrite it.
    """
    if not github.repo_access(repo).visible:
        raise RepoBusy(UNREADABLE_REPOSITORY)
    return parse_gojira_yaml(github.read_file(repo, GOJIRA_YAML))


def read_repo_env(github: GitHubClient, repo: str) -> dict[str, str]:
    """The environment variables `repo`'s `gojira.yaml` carries, by a plain
    file read rather than a clone.

    A read every time the panel opens should not cost a clone, and the
    GitHub read gives exactly what the default branch holds, which is what
    the panel is showing. `edit_gojira_yaml` still clones, because a write
    has to push what it produced.
    """
    return read_env(read_default_gojira(github, repo))


def _skip_ci(message: str) -> str:
    """The commit message with a trailing `[skip ci]`, added once."""
    return message if "[skip ci]" in message else f"{message} [skip ci]"


def edit_gojira_yaml(
    github: GitHubClient,
    gojira: GojiraClient,
    *,
    repo: str,
    connected: bool,
    gojira_app_id: int | None,
    edit: Callable[[dict], bool],
    message: str,
    branch: str = ENV_BRANCH,
    title: str = ENV_PR_TITLE,
    body: str = ENV_PR_BODY,
    carries: Callable[[dict], bool] | None = None,
    already_open: str = ENV_PR_ALREADY_OPEN,
    reprovision: bool = True,
) -> RepoEdit:
    """Clone `repo`, run `edit` over its parsed `gojira.yaml`, and get the
    result in front of the deploy platform the way this KIND of agent
    allows.

    `edit` returns whether it changed anything. An edit that changed
    nothing still asks the platform to regenerate (composed agents only):
    a reprovision is cheap and idempotent, and the case that matters is a
    save whose push landed and whose reprovision then failed -- a retry
    has nothing left to push, and refusing to reprovision would leave that
    change undeployed forever. The answer says `unchanged`, and the route
    says a deploy was triggered anyway.

    A COMPOSED agent's repository is the room's, so the change is committed
    to its default branch. The reprovision is fired and not waited on,
    unlike `kobo_api.slack_setup.wire_slack`: that call is finishing a
    setup somebody is watching, and this one is a settings save that should
    not hold a browser open for up to five minutes. What the panel promises
    is that the change reaches the agent the next time it starts, which is
    true the moment the push lands.

    A CONNECTED agent's repository belongs to its team, and the room never
    pushes to its branches (design 2026-09-21). The change is proposed on
    `branch` instead and the pull request's link comes back for the owner
    to merge. `carries`, when given, is asked of the gojira.yaml AT THAT
    BRANCH's head: a pull request whose branch already holds this exact
    change is the answer rather than a refusal, and one that does not is
    in the way. Without it, any open pull request on the branch is in the
    way.

    Raises `ClientError` for anything the repository or the platform
    refuses; no secret value passes through here at all.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)

        gojira_yaml_path = workdir / "gojira.yaml"
        if not gojira_yaml_path.exists():
            raise RepoBusy(MISSING_GOJIRA_YAML)
        try:
            text = gojira_yaml_path.read_text()
        except UnicodeDecodeError as exc:
            raise RepoBusy(UNREADABLE_GOJIRA_YAML) from exc
        data = parse_gojira_yaml(text)
        changed_here = edit(data)

        if connected:
            if not changed_here:
                return RepoEdit(how=UNCHANGED)
            gojira_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))
            return _propose(
                github,
                workdir,
                repo,
                branch=branch,
                title=title,
                body=body,
                carries=carries,
                already_open=already_open,
            )

        pushed = False
        if changed_here:
            gojira_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))
            # Marked [skip ci]: a commit that changes only gojira.yaml fails the
            # generated workflow's contract check (the manifests no longer match
            # the contract), and the deploy that matters is the setup pull
            # request the reprovision below makes Gojira open. One push, no
            # failed run (agora discussion #55, item 2).
            pushed = github.commit_push(workdir, _skip_ci(message))

    if reprovision and gojira_app_id is not None:
        # Outside the clone, and fired whether or not anything was pushed.
        # A reprovision is idempotent and cheap; the expensive case is the
        # save whose push landed and whose reprovision failed, where a
        # retry has nothing to push and would otherwise never deploy.
        #
        # `reprovision=False` is for the one caller that has more to do
        # before the platform should regenerate: removing an agent's last
        # secret commits the line away FIRST, then deletes the Secret, and
        # only then asks for a deploy -- see the secrets route.
        try:
            gojira.reprovision(gojira_app_id)
        except ClientError as exc:
            # Named apart from every other failure here, because what a
            # person should be told depends on whether their file changed.
            raise DeployTriggerFailed(str(exc), pushed=pushed) from exc
    return RepoEdit(how=COMMITTED if pushed else UNCHANGED)


def _propose(
    github: GitHubClient,
    workdir: Path,
    repo: str,
    *,
    branch: str,
    title: str,
    body: str,
    carries: Callable[[dict], bool] | None,
    already_open: str,
    files: frozenset[str] | None = None,
) -> RepoEdit:
    """The connected half of `edit_gojira_yaml`: one pull request on
    `branch`, or a plain sentence saying what is in the way. `files` is what
    a leftover branch may touch and still be the room's to recycle, for the
    one caller whose request writes more than gojira.yaml
    (`kobo_api.connected_wiring.offer`)."""
    open_pr = github.find_pr(repo, branch=branch)
    if open_pr is not None:
        # An open pull request is only "already carrying this" if its own
        # branch actually says so. Assuming it did was the bug: a pull
        # request opened for one change was reported as proposing another
        # it had never seen.
        if carries is not None and carries(
            parse_gojira_yaml(github.read_file(repo, "gojira.yaml", ref=branch))
        ):
            return RepoEdit(how=PULL_REQUEST, pull_request=open_pr)
        raise RepoBusy(f"{already_open}{open_pr}")

    # No pull request, but the branch may still be there: a closed one
    # leaves it behind, and so does a merged one in a repository that does
    # not delete merged branches. A branch the ROOM made, with nothing
    # open from it, is the room's to recycle -- refusing would leave the
    # panel permanently unable to save on a repository whose owner merged
    # the room's last pull request without ticking delete-branch.
    if github.branch_exists(repo, branch):
        if files is None:
            require_our_branch(github, repo, branch)
        else:
            require_our_branch(github, repo, branch, files)
        log.info("recycling the room's own leftover branch %s in %s", branch, repo)
        github.delete_branch(repo, branch)

    url = github.open_pr(workdir, repo=repo, branch=branch, title=title, body=body)
    return RepoEdit(how=PULL_REQUEST, pull_request=url)


def require_our_branch(github: GitHubClient, repo: str, branch: str, files: frozenset[str] = frozenset({GOJIRA_YAML})) -> None:
    """Raise unless `branch` is one the room made, and only the room.

    The room deletes this branch, and a name is not ownership: a team that
    happens to use `kobo/env` for something of their own would have that
    work destroyed by a save in a panel they never opened. So the branch
    has to prove itself against the default branch, on two counts, and
    both have to hold:

    - it touches `files` and nothing else (`gojira.yaml` by default, the
      only file either of the room's two agent-repo edits writes; a gateway
      config edit names its own file); and
    - every commit on it is authored by the room's own commit identity,
      because a person's commit on this branch means a person has been
      working here.

    A comparison that could not be made at all (`found` false) is a
    refusal too, and that is the important half: an empty file list from a
    failed lookup and an empty file list from an identical branch are the
    same value, so treating "cannot tell" as "fine" would delete exactly
    the branches this check exists to protect.
    """
    comparison = github.compare(repo, DEFAULT_BRANCH_REF, branch)
    if not comparison.found:
        raise RepoBusy(branch_not_ours(branch))
    if set(comparison.files) - set(files):
        raise RepoBusy(branch_not_ours(branch))
    if any(author.lower() != COMMIT_AUTHOR_EMAIL.lower() for author in comparison.authors):
        raise RepoBusy(branch_not_ours(branch))
