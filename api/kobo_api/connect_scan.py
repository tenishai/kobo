"""What the room can honestly say about a repository it has been asked to
connect, before it touches anything (connected-agents design 2026-09-21).

Read-only by construction: five GitHub reads through the contents and
branches endpoints, one database question, and no clone. It runs twice for
every connect -- once for the form's own report, once inside the connect
route -- because a report the browser holds is a claim about a repository
five minutes ago, and the route has to answer for the repository now.

The two lists are not interchangeable. A BLOCKER is something the room
cannot work around: the connect would fail, or would deploy the wrong
thing, so Connect stays disabled and the route refuses. A WARNING is
something true about the repository that its owner should know and may
still choose. Nothing here is a policy the room invents; every warning is
either a fact about what will happen next (a protected branch means a
person merges the wiring) or a fact about the repository the owner can
check for themselves.

Each finding carries a code and a sentence. The code is what the UI keys
on; the sentence is what a person reads, and it is the same text whether it
arrives in a scan report or in the connect route's refusal, so it never
says anything a viewer of the agent could not be told.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import yaml
from sqlalchemy.orm import Session

from kobo_api.clients import GitHubClient, GojiraClient
from kobo_api.config import Config
from kobo_api.kinds import RUN_MODE_HEADLESS, RUN_MODE_HTTP
from kobo_api.models import Agent
from kobo_api.repo_name import is_in_org
from kobo_api.runtime import registry_root_of
from kobo_api.steps import HEADLESS_UNAVAILABLE

log = logging.getLogger(__name__)

# Blocker codes.
REPO_NOT_IN_ORG = "repo_not_in_org"
REPO_NOT_VISIBLE = "repo_not_visible"
REPO_NOT_WRITABLE = "repo_not_writable"
NO_DOCKERFILE = "no_dockerfile"
ALREADY_DEPLOYED = "already_deployed"
NAME_TAKEN = "name_taken"
# Named with a suffix because the SENTENCE it carries is `steps.
# HEADLESS_UNAVAILABLE`, imported rather than rewritten: the step and the
# scan are saying the same thing, and two copies of one sentence is how
# they stop saying it the same way.
HEADLESS_UNAVAILABLE_CODE = "headless_unavailable"

# Warning codes.
BASE_NOT_PINNED = "base_not_pinned"
BASE_OUTSIDE_REGISTRY = "base_outside_registry"
ENV_FILE_COMMITTED = "env_file_committed"
BRANCH_PROTECTED = "branch_protected"
LOOKS_COMPOSED = "looks_composed"
NO_PORT_EXPOSED = "no_port_exposed"

# Everything the room will ever write into a team's repository, in the
# order the report lists it. The service account manifest is folded into
# the k8s line rather than given one of its own: it is the only file in
# there anyone asks about, and a three-line list is one a person reads.
ADDS = [
    "gojira.yaml",
    "k8s/ (including k8s/base/serviceaccount.yaml)",
    ".github/workflows/ (one CI workflow)",
]

# The one thing the room cannot check, said in the report rather than left
# for somebody to discover. The room's GitHub App can write to every
# repository in the organisation it is installed on, and the token it holds
# cannot see who the caller is on GitHub, so ownership is the caller's word
# plus the connect route's own record of who gave it. The design's "What
# this does not do" says the same thing at length; this is the sentence the
# form shows above the list of what will be added.
ADDS_PREAMBLE = (
    "The room cannot check who owns this repository, so connect it only if "
    "it is yours or its owner agrees; whoever connects it is recorded."
)

# The port the deploy platform's ordinary class probes. Only checked to
# warn early; the deploy proves it either way.
HTTP_PORT = "8080"

DOCKERFILE = "Dockerfile"
GOJIRA_YAML = "gojira.yaml"
# Where a repository that already deploys says what it is called. The
# deploy platform regenerates this file under the AGENT's name, so the two
# names have to agree before anything is written (see `_scan_identity`).
BASE_DEPLOYMENT = "k8s/base/deployment.yaml"
REPO_NAME_MISMATCH = "repo_name_mismatch"
SERVICE_ACCOUNT_CHANGES = "service_account_changes"
ENV_FILE = ".env"
BUNDLE_DIR = "bundle"

# `FROM <ref>` with an optional `AS <stage>`. Stage names are collected so
# a later `FROM builder` is recognised as an internal reference rather than
# judged as a base image nobody can pin.
_FROM_RE = re.compile(
    r"^[ \t]*FROM[ \t]+(?:--\S+[ \t]+)*(?P<ref>\S+)(?:[ \t]+AS[ \t]+(?P<stage>\S+))?",
    re.IGNORECASE | re.MULTILINE,
)
_EXPOSE_RE = re.compile(r"^[ \t]*EXPOSE[ \t]+(?P<ports>.+)$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class Finding:
    code: str
    message: str


@dataclass
class ScanReport:
    blockers: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    adds: list[str] = field(default_factory=lambda: list(ADDS))
    adds_preamble: str = ADDS_PREAMBLE
    # GitHub's own spelling of the repository, once it has answered for it;
    # the name as given until then. This is what the connect route records,
    # so one repository cannot become two rows apart from each other's case.
    repo: str = ""
    # The name the repository already deploys under (its base Deployment's
    # metadata.name), when it has one. The form prefills and locks the Name
    # field to it, and the connect route refuses any other name: a connected
    # repository keeps its own name (Kyle's ruling, 2026-09-29). None for a
    # repository with no k8s/base/deployment.yaml, where the typed name
    # stands because there is nothing for it to disagree with.
    repo_name: str | None = None
    # Its service account, read from the same Deployment, for the report.
    repo_service_account: str | None = None
    # The first container's first containerPort in that Deployment, if any.
    repo_port: int | None = None
    # Whether the Dockerfile EXPOSEs the port the platform probes; None until
    # the Dockerfile has been read.
    dockerfile_exposes_http: bool | None = None
    # How the agent runs, decided by the scan (2026-09-29: the form no longer
    # asks): `http` when the repository says it listens (a containerPort in
    # its Deployment, or an EXPOSE in its Dockerfile), else `headless`. A
    # caller may still say so explicitly and is believed.
    run_mode: str = RUN_MODE_HTTP


def scan_repo(
    github: GitHubClient,
    gojira: GojiraClient,
    session: Session,
    config: Config,
    *,
    repo: str,
    name: str,
    run_mode: str | None = None,
) -> ScanReport:
    """Everything the room knows about connecting `repo` as `name`.

    `run_mode` None means "decide from the repository" (see
    `ScanReport.run_mode`); a value is a caller's explicit word.

    `gojira` is taken and not consulted. The deploy platform exposes no
    lookup by repository -- its API creates an app and answers 409 when one
    already exists for that repository, and there is no route that asks the
    question without creating anything -- so the one thing the room can see
    from outside is a `gojira.yaml` in the repository itself naming a
    different app. The rest of that case lands on the onboard step, which
    already turns the platform's 409 into a sentence. The parameter stays
    because the answer changes the day the platform grows such a route, and
    every caller already has the client in hand.

    Never raises for anything about the repository: an unreachable
    repository, an absent file and an unparseable one are all findings or
    silence, because this answers a form. A client failure that says
    nothing about this repository (a bad token) still propagates, and the
    route answers it the way it answers any other backend failure.
    """
    repo = repo.strip()
    report = ScanReport(repo=repo)

    if is_in_org(repo, config.github_org):
        _scan_the_repository(
            github, session, config, report, repo=repo, name=name, run_mode=run_mode
        )
    else:
        report.blockers.append(
            Finding(
                REPO_NOT_IN_ORG,
                f"{repo} is not a repository in {config.github_org}. This "
                f"room can only connect repositories in {config.github_org}.",
            )
        )

    # Last, and always asked: these two are about the room, not the
    # repository, so they are worth saying even when the repository could
    # not be read at all.
    if session.query(Agent).filter_by(name=name).first() is not None:
        report.blockers.append(
            Finding(NAME_TAKEN, f"The name {name} is already taken in this room.")
        )
    if report.run_mode == RUN_MODE_HEADLESS and not config.connected_headless_class:
        report.blockers.append(Finding(HEADLESS_UNAVAILABLE_CODE, HEADLESS_UNAVAILABLE))

    return report


def _scan_the_repository(
    github: GitHubClient,
    session: Session,
    config: Config,
    report: ScanReport,
    *,
    repo: str,
    name: str,
    run_mode: str | None,
) -> None:
    """The reads, in the order a person would make them, stopping the moment
    an answer makes the next question meaningless."""
    access = github.repo_access(repo)
    if access.full_name:
        report.repo = access.full_name
    if not access.visible:
        report.blockers.append(
            Finding(
                REPO_NOT_VISIBLE,
                f"{repo} could not be reached. Check the name, and that this "
                "room has been given access to it.",
            )
        )
        return

    # Asked once the canonical name is known, so the same repository under
    # two spellings is still one repository. One repository is one agent
    # inside this room as much as outside it: two rows pointing at one
    # repository would deploy over each other, and destroying either would
    # act on the other's app.
    held_by = (
        session.query(Agent).filter_by(repo_full_name=report.repo).first()
    )
    if held_by is not None:
        report.blockers.append(
            Finding(
                ALREADY_DEPLOYED,
                f"{report.repo} is already connected in this room as "
                f"{held_by.name}.",
            )
        )

    if not access.can_write:
        # Not a reason to stop reading: an owner fixing access wants the
        # rest of the report in the same breath.
        report.blockers.append(
            Finding(
                REPO_NOT_WRITABLE,
                f"This room can read {repo} but cannot change it. It needs "
                "write access to open the setup pull request.",
            )
        )

    # One listing answers four of the questions below, which is why it is a
    # listing and not four existence checks.
    entries = set(github.list_root(repo))

    if DOCKERFILE in entries:
        _scan_dockerfile(github, config, report, repo=repo)
    else:
        report.blockers.append(
            Finding(
                NO_DOCKERFILE,
                "There is no Dockerfile at the root of this repository. The "
                "deploy platform builds the image from one.",
            )
        )

    if GOJIRA_YAML in entries:
        _scan_gojira_yaml(github, report, repo=repo, name=name)
    # Not gated on the listing: the file sits two directories down, and one
    # read that answers None is the same cost as an existence check.
    _scan_identity(github, report, repo=repo, name=name)
    _decide_run_mode(report, requested=run_mode)

    if ENV_FILE in entries:
        report.warnings.append(
            Finding(
                ENV_FILE_COMMITTED,
                "A .env file is committed here. Everyone who can read this "
                "repository can read what is in it.",
            )
        )

    if BUNDLE_DIR in entries:
        report.warnings.append(
            Finding(
                LOOKS_COMPOSED,
                "This repository has a bundle directory, so it looks like an "
                "agent this room built. Connecting it means the room will "
                "stop editing it.",
            )
        )

    if access.default_branch and github.branch_protected(repo, access.default_branch):
        report.warnings.append(
            Finding(
                BRANCH_PROTECTED,
                f"The default branch ({access.default_branch}) is protected, "
                "so someone on your team will have to merge the setup pull "
                "request.",
            )
        )


def _scan_dockerfile(
    github: GitHubClient,
    config: Config,
    report: ScanReport,
    *,
    repo: str,
) -> None:
    text = github.read_file(repo, DOCKERFILE)
    if text is None:
        # Listed at the root a moment ago and unreadable now: a directory
        # named Dockerfile, something enormous, or a push in between. There
        # is nothing to say about a file the room could not read, and the
        # build will say it plainly enough.
        log.info("connect scan could not read the Dockerfile in %s", repo)
        return

    base = _base_image(text)
    if base is not None and _judgeable(base):
        if "@sha256:" not in base:
            report.warnings.append(
                Finding(
                    BASE_NOT_PINNED,
                    "The image this one builds on is not pinned to an exact "
                    "version. A moving base is how a known vulnerability "
                    "shipped here once already.",
                )
            )
        registry_root = registry_root_of(config.runtime_image)
        if not base.startswith(registry_root):
            report.warnings.append(
                Finding(
                    BASE_OUTSIDE_REGISTRY,
                    f"The image this one builds on does not come from "
                    f"{registry_root}, which is where this room's images "
                    "come from.",
                )
            )

    report.dockerfile_exposes_http = _exposes_http_port(text)


def _scan_gojira_yaml(
    github: GitHubClient, report: ScanReport, *, repo: str, name: str
) -> None:
    text = github.read_file(repo, GOJIRA_YAML)
    if text is None:
        return
    try:
        data = yaml.safe_load(text) or {}
    except (yaml.YAMLError, RecursionError):
        # A file the room cannot read says nothing about which app this
        # repository is, and the room does not refuse a connect on a guess.
        # RecursionError as well as YAMLError: a document nested past the
        # parser's own limit is a file in somebody else's repository, and
        # it must read as unparseable rather than as a 500 in front of
        # whoever is filling in the form.
        log.info("connect scan could not read the gojira.yaml in %s", repo)
        return
    if not isinstance(data, dict):
        return
    app = data.get("app")
    if isinstance(app, str) and app and app != name:
        report.blockers.append(
            Finding(
                ALREADY_DEPLOYED,
                f"This repository is already set up to deploy as {app}. One "
                f"repository is one agent, so connect it as {app} or remove "
                "its gojira.yaml first.",
            )
        )


def _scan_identity(
    github: GitHubClient, report: ScanReport, *, repo: str, name: str
) -> None:
    """A repository that already deploys has a name, and the room does not
    rename it.

    The deploy platform regenerates `k8s/base` under the agent's name. Its
    overlays keep targeting the old one (image substitutions, patches, a
    production overlay's namespace), so a rename orphans every one of them
    and the next deploy of the environment the room does not manage breaks.
    The first repository connected this way had exactly that shape
    (2026-09-25). So the base Deployment's `metadata.name` IS the agent's
    name: the form takes it, and a different name is a blocker that says
    what to do instead. Renaming is a reviewed change in the repository,
    never a side effect of connecting it.

    Unreadable or unparseable is silence, the way `_scan_gojira_yaml`
    treats its file: the room refuses on facts, not on guesses.
    """
    text = github.read_file(repo, BASE_DEPLOYMENT)
    if text is None:
        return
    try:
        docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except (yaml.YAMLError, RecursionError):
        log.info("connect scan could not read %s in %s", BASE_DEPLOYMENT, repo)
        return
    deployment = next((d for d in docs if d.get("kind") == "Deployment"), None)
    if deployment is None:
        return
    metadata = deployment.get("metadata") or {}
    repo_name = metadata.get("name") if isinstance(metadata, dict) else None
    if not isinstance(repo_name, str) or not repo_name:
        return
    report.repo_name = repo_name
    pod = ((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}
    service_account = pod.get("serviceAccountName") if isinstance(pod, dict) else None
    if isinstance(service_account, str) and service_account:
        report.repo_service_account = service_account
        # 1:1 naming (ruling 2026-09-30): the agent, its service account and
        # everything derived from the name follow the repository's name. The
        # setup pull request writes `service_account: agent-<name>` into
        # gojira.yaml, and the deploy platform reads the old deployment's
        # account only when there is no gojira.yaml, so the regenerated base
        # runs as the room's account. That is right, and it is a change the
        # team should see coming: anything granted to the old account by
        # hand does not follow it.
        expected_account = f"agent-{repo_name}"
        if service_account != expected_account:
            report.warnings.append(
                Finding(
                    SERVICE_ACCOUNT_CHANGES,
                    f"This repository deploys as service account {service_account} "
                    f"({BASE_DEPLOYMENT}). The setup pull request moves it to "
                    f"{expected_account}, the account the room grants model and "
                    f"cloud access to. Anything granted to {service_account} by "
                    "hand does not follow; review that line before merging.",
                )
            )
    containers = pod.get("containers") if isinstance(pod, dict) else None
    first = containers[0] if isinstance(containers, list) and containers else {}
    ports = first.get("ports") if isinstance(first, dict) else None
    for entry in ports or []:
        port = entry.get("containerPort") if isinstance(entry, dict) else None
        if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
            report.repo_port = port
            break
    if name and name != repo_name:
        report.blockers.append(
            Finding(
                REPO_NAME_MISMATCH,
                f"This repository already deploys as {repo_name} "
                f"({BASE_DEPLOYMENT}). Connect it under that name, or rename it "
                "in the repository first. The room never renames a repository "
                "it did not write.",
            )
        )


def _decide_run_mode(report: ScanReport, *, requested: str | None) -> None:
    """How the agent runs, from what the repository says, unless a caller
    said so themselves.

    The form used to ask (2026-09-29 it stopped): a person choosing between
    "listens on a port" and "does not" was answering a question the
    repository already answers, and could answer it wrong. A containerPort
    in the base Deployment or an EXPOSE in the Dockerfile means the platform
    can probe it; nothing of either means a worker or a chat bot, which the
    room hosts as headless when it has a class for that (the blocker at the
    end of `scan_repo` says when it does not).

    An explicit `requested` mode is kept, with the old warning when it
    claims a port the repository does not show.
    """
    listens = report.repo_port is not None or bool(report.dockerfile_exposes_http)
    if requested is not None:
        report.run_mode = requested
        if requested != RUN_MODE_HEADLESS and not listens:
            report.warnings.append(
                Finding(
                    NO_PORT_EXPOSED,
                    f"Nothing in the repository opens port {HTTP_PORT}. The "
                    "deploy will show whether it serves one either way.",
                )
            )
        return
    report.run_mode = RUN_MODE_HTTP if listens else RUN_MODE_HEADLESS


def _base_image(dockerfile_text: str) -> str | None:
    """The image the FINAL stage builds on, or None when there is no FROM
    line at all.

    Earlier stages never run in the cluster, so the last one is the only
    one worth judging -- and a final `FROM builder` names a stage declared
    further up rather than an image, so the stage it names is followed,
    not merely the nearest earlier line that happens to hold an image ref.
    A three-stage file whose last FROM names the FIRST stage builds on the
    first stage's image, and that is the one this answers with.

    A reference that loops back on itself cannot be resolved and is not
    worth chasing: the loop is bounded by the number of stages, and the
    answer is None.
    """
    matches = list(_FROM_RE.finditer(dockerfile_text))
    if not matches:
        return None
    stage_refs = {
        match.group("stage").lower(): match.group("ref")
        for match in matches
        if match.group("stage")
    }
    ref = matches[-1].group("ref")
    for _ in range(len(matches)):
        following = stage_refs.get(ref.lower())
        if following is None:
            return ref
        if following.lower() == ref.lower():
            return None
        ref = following
    return None


def _judgeable(base: str) -> bool:
    """Whether the room can say anything true about this base image.

    `scratch` is the empty image: there is nothing to pin and no registry
    to host it. A ref that starts with a build argument is not resolved
    until build time, and the room does not have its value. Warning about
    either would be saying something the room does not know.
    """
    return base != "scratch" and not base.startswith("$")


def _exposes_http_port(dockerfile_text: str) -> bool:
    """Whether any EXPOSE line names the port the platform probes. A line
    may carry several ports, and a port may carry a protocol (`8080/tcp`).
    """
    for match in _EXPOSE_RE.finditer(dockerfile_text):
        for port in match.group("ports").split():
            if port.split("/", 1)[0] == HTTP_PORT:
                return True
    return False
