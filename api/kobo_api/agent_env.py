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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from kobo_api.clients import ClientError, GitHubClient, GojiraClient

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

# The two model-access variables `kobo_api.steps._set_service_account_and_
# model_env` writes into every agent's `gojira.yaml`. Spelled out here
# rather than imported from that module: this is the list of names an owner
# may not take, which is a decision about the panel, and importing would
# put a cycle between this module and the step engine that reaches it.
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
        *MODEL_ENV_KEYS,
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


def key_refusal(key: str) -> str | None:
    """The plain sentence refusing `key`, or None when it is allowed.

    Checked in the order a person would: is this a name at all, is it a
    reasonable length, and is it one the room already fills in. The
    reserved check comes last so a mistyped reserved name reads as a
    mistyped name, which is what it is.
    """
    if not KEY_RE.match(key):
        return (
            f"{key or 'that name'}: use capital letters, digits and "
            "underscores, starting with a letter"
        )
    if len(key) > MAX_KEY_LENGTH:
        return f"{key}: keep a name to {MAX_KEY_LENGTH} characters or fewer"
    if key in RESERVED_KEYS:
        return f"{key}: {ROOM_SETS_THIS_ONE}"
    return None


def write_refusal(
    values: dict[str, str], remove: list[str], existing: list[str]
) -> str | None:
    """The plain sentence refusing one whole write, or None to go ahead.

    `values` is what the caller is setting, `remove` what they are taking
    away, and `existing` the key names already held -- names only, never
    values, which is all the caller of this ever has for the secrets list
    anyway.

    Four refusals, in the order that answers a person best: a name that is
    not allowed (either side, so a remove is checked exactly as a set is),
    a key named on both sides at once (which of the two was meant is not
    something to guess at), and a write that would leave more keys behind
    than a list may hold.
    """
    for key in list(values) + list(remove):
        refusal = key_refusal(key)
        if refusal is not None:
            return refusal

    both = sorted(set(values) & set(remove))
    if both:
        return f"{both[0]} is in both the add list and the remove list"

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
    `gojira.yaml` -- never the two the room wires in itself.

    A non-mapping `env` reads as empty rather than raising: a file shaped
    that way could never have run on the deploy platform, so there is
    nothing in it to show, and a read is not the place to say so.

    Values are rendered as text because YAML gives back an int for
    `PORT: 8080` and a bool for `DEBUG: true`, and what the panel edits is
    a text field. `True` renders as `true`, the way the file spelled it.
    """
    env = data.get("env")
    if not isinstance(env, dict):
        return {}
    return {
        str(key): _as_text(value)
        for key, value in env.items()
        if str(key) not in MODEL_ENV_KEYS
    }


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
    return isinstance(existing, list) and entry in existing


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

ENV_PR_TITLE = "kobo: environment variables"
SECRETS_WIRING_PR_TITLE = "kobo: read the agent's own secrets"

ENV_PR_BODY = (
    "This changes the environment variables the agent runs with.\n\n"
    "Files changed:\n"
    "- gojira.yaml: the `env` mapping. These are values that are not "
    "sensitive; secrets are held in the secrets platform and never appear "
    "in this file.\n\n"
    "gojira.yaml is rewritten from its parsed form, so comments and key "
    "order in that one file are not preserved.\n"
)

SECRETS_WIRING_PR_BODY = (
    "This lets the agent read the secrets its owner added in the room.\n\n"
    "Files changed:\n"
    "- gojira.yaml: one entry added to `secrets_from`, naming the "
    "Kubernetes Secret the secrets platform delivers into this agent's "
    "namespace. Secret VALUES are not here and never will be: this file "
    "only names the Secret.\n\n"
    "gojira.yaml is rewritten from its parsed form, so comments and key "
    "order in that one file are not preserved.\n"
)

# What the room says when a pull request it opened before is still open on
# the same branch and this save is not the same change. Pushing the branch
# again would fail non fast-forward, and reusing the open pull request
# would report a change as proposed that is not in it.
ENV_PR_ALREADY_OPEN = (
    "a pull request with this agent's environment is already open. Merge or "
    "close it, then save again: "
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


def read_repo_env(github: GitHubClient, repo: str) -> dict[str, str]:
    """The environment variables `repo`'s `gojira.yaml` carries, by a plain
    file read rather than a clone.

    A read every time the panel opens should not cost a clone, and the
    GitHub read gives exactly what the default branch holds, which is what
    the panel is showing. `edit_gojira_yaml` still clones, because a write
    has to push what it produced.

    Permission is checked FIRST, the way the connect scan does, because
    `GitHubClient.read_file` answers None for a file the room may not read
    exactly as it does for a file that is not there. Those are different
    answers to a person -- one is "add the file", the other is "the room
    lost access to this repository" -- and collapsing them would send
    somebody hunting for a file sitting in front of them.

    A missing or empty file reads as no variables. An unparseable one is
    `RepoBusy`, not an empty read: showing an empty list over a file
    nobody here could parse invites a save that would overwrite it.
    """
    if not github.repo_access(repo).visible:
        raise RepoBusy(UNREADABLE_REPOSITORY)
    return read_env(parse_gojira_yaml(github.read_file(repo, "gojira.yaml")))


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
            )

        pushed = False
        if changed_here:
            gojira_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))
            pushed = github.commit_push(workdir, message)

    if gojira_app_id is not None:
        # Outside the clone, and fired whether or not anything was pushed.
        # A reprovision is idempotent and cheap; the expensive case is the
        # save whose push landed and whose reprovision failed, where a
        # retry has nothing to push and would otherwise never deploy.
        gojira.reprovision(gojira_app_id)
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
) -> RepoEdit:
    """The connected half of `edit_gojira_yaml`: one pull request on
    `branch`, or a plain sentence saying what is in the way."""
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
        raise RepoBusy(f"{ENV_PR_ALREADY_OPEN}{open_pr}")

    # No pull request, but the branch may still be there: a closed one
    # leaves it behind, and so does a merged one in a repository that does
    # not delete merged branches. This branch is one the room named itself
    # and nothing is open from it, so it is the room's to recycle --
    # refusing here would leave the panel permanently unable to save on a
    # repository whose owner merged the room's last pull request and did
    # not tick the delete-branch box.
    if github.branch_exists(repo, branch):
        log.info("recycling the room's own leftover branch %s in %s", branch, repo)
        github.delete_branch(repo, branch)

    url = github.open_pr(workdir, repo=repo, branch=branch, title=title, body=body)
    return RepoEdit(how=PULL_REQUEST, pull_request=url)
