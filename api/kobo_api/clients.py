"""Platform clients for kobo-api.

Every operation the provisioner performs against the outside world (GitHub,
gojira, GCP, an already-deployed agent's a2a endpoint, kubectl) sits behind a
`typing.Protocol` here, so provisioning logic never depends on a concrete
backend directly — only on these shapes. This mirrors gojira's own
GCPClient/KubectlClient/GitHubClient seam (`gojira/gcp.py`, `gojira/k8s.py`,
`gojira/github.py`): steps are testable against in-memory fakes
(`tests/fakes.py`) and the real backend is swappable.

`CommandRunner` is gojira's shell seam (`gojira/shell.py`) copied in spirit,
with one difference: it folds the raise-on-failure decision into the runner
itself (`run()` raises `ClientError` on a non-zero exit) rather than handing
back a raw return code for every caller to check. Callers that need to
tolerate one particular failure (e.g. a describe-then-create existence check,
or gcloud's "already a member" on a repeat group-add) catch `ClientError` and
inspect its `.stderr`.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict, runtime_checkable
from urllib.parse import quote

import httpx

from kobo_api.config import Config, GroupChoice
from kobo_api.groups import GroupResolver, OktaGroupResolver, OktaPeopleSearch

if TYPE_CHECKING:
    # kobo_api.ladon and kobo_api.github_app import ClientError from this
    # module, so these imports exist only for annotations (`Clients.ladon`,
    # `installation_token_provider`) -- `from __future__ import annotations`
    # keeps them from being evaluated at runtime, and the implementations
    # are imported at call time.
    from kobo_api.github_app import InstallationTokenProvider
    from kobo_api.ladon import LadonClient

log = logging.getLogger(__name__)

# git commit author for commits kobo makes on the caller's behalf (setup
# commits, artifact pushes) — explicit so it works in environments with no
# git user configured, mirroring gojira's GhCLI (gojira/github.py).
COMMIT_AUTHOR_NAME = "Kobo"
COMMIT_AUTHOR_EMAIL = "noreply@kobo.dev"


class ClientError(RuntimeError):
    """A shelled-out command or platform API call failed."""

    def __init__(
        self, message: str, *, returncode: int = 1, stdout: str = "", stderr: str = ""
    ):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class GojiraConflict(RuntimeError):
    """gojira already has an app connected for this repo (409 on create)."""


class SlackApiError(ClientError):
    """Slack's Web API returned `ok: false` (a non-auth failure, or a token
    rotation attempt that itself failed) — a ClientError subclass so it's
    caught by the same 502 mapping every other ClientError gets, without
    routes needing a Slack-specific except clause.
    """


class StorageNotFound(ClientError):
    """404 from object storage — callers branch on 'absent' vs 'broken'."""


class CabalError(ClientError):
    """The switchboard (cabal)'s admin API returned a failure `CabalClient`
    doesn't otherwise tolerate (graduate/absorb's non-2xx, or relay's) — a
    `ClientError` subclass, same family as `SlackApiError`/`StorageNotFound`,
    so any call site that only knows the general `ClientError` contract
    keeps working unchanged, while a caller that cares specifically about
    the switchboard can still catch this by name.
    """


class CommandRunner:
    """Runs a subprocess command; returns stdout, raises ClientError on rc != 0."""

    def run(
        self,
        args: list[str],
        stdin: str | None = None,
        *,
        redact_args: bool = False,
        env: dict[str, str] | None = None,
    ) -> str:
        # Final-review Finding 3: this debug line used to log the full argv
        # unconditionally — for `KubectlCLI.apply_secret`'s stage-1 call,
        # that argv carries `--from-literal=SLACK_*_TOKEN=<value>`, so one
        # `--log-level debug` away put pasted Slack tokens straight into
        # logs. `redact_args=True` (passed by `apply_secret`, both stages)
        # logs a fixed placeholder instead of the real argv; every other
        # caller is unaffected (default False, unchanged behavior).
        #
        # `env` (Task 3: GitHub App installation tokens) is the same kind of
        # secret-bearing input, carried a different way — as a subprocess
        # env var (GhCLI's GH_TOKEN) rather than argv — so it is NEVER
        # logged here, at any level, under any flag. When set, the
        # subprocess gets `{**os.environ, **env}`; when None (the default),
        # `subprocess.run` gets no `env=` kwarg at all, so behavior for
        # every existing caller is byte-identical to before this parameter
        # existed.
        log.debug("exec: %s", "<redacted argv>" if redact_args else " ".join(args))
        try:
            proc = subprocess.run(
                args,
                input=stdin,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, **env} if env else None,
            )
        except FileNotFoundError as exc:
            raise ClientError(f"{args[0]}: not found on PATH") from exc
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        if proc.returncode != 0:
            raise ClientError(
                f"{' '.join(args)} failed (rc={proc.returncode}): {stderr or stdout}",
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        return stdout

    def run_bytes(
        self,
        args: list[str],
        stdin: bytes | None = None,
        *,
        redact_args: bool = False,
        env: dict[str, str] | None = None,
    ) -> bytes:
        """Like `run()`, but for commands whose stdout is binary (e.g. `git
        archive`, kobo_api.composer's skill-pinning materialization) —
        `run()`'s `text=True` decode-and-strip would corrupt a tar archive's
        bytes, so this skips both and hands back stdout untouched. Still
        raises ClientError on rc != 0, decoding stderr/stdout only for the
        error message.

        `redact_args` is the same debug-log seam as `run()`'s (see there for
        the rationale) — carried here too so any future binary-stdout caller
        with secret-bearing argv gets the same option, even though no
        current caller of `run_bytes` needs it. `env` is the same overlay as
        `run()`'s too, same never-logged rule.
        """
        log.debug("exec (bytes): %s", "<redacted argv>" if redact_args else " ".join(args))
        try:
            proc = subprocess.run(
                args,
                input=stdin,
                capture_output=True,
                text=False,
                check=False,
                env={**os.environ, **env} if env else None,
            )
        except FileNotFoundError as exc:
            raise ClientError(f"{args[0]}: not found on PATH") from exc
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            stdout = proc.stdout.decode(errors="replace").strip()
            raise ClientError(
                f"{' '.join(args)} failed (rc={proc.returncode}): {stderr or stdout}",
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        return proc.stdout


# --- GitHub -------------------------------------------------------------------


# The largest file the contents reader will decode. The connect scan reads
# a Dockerfile and a gojira.yaml -- both a few hundred bytes in every real
# repository -- and this transport has no other callers. A cap keeps a
# repository with something enormous at one of those paths from being
# base64-decoded into this process at all; the scan reads the answer as
# "no such file", which is the honest one for a path it cannot reason
# about.
MAX_READ_FILE_BYTES = 256 * 1024


@dataclass(frozen=True)
class BranchComparison:
    """What one branch changed against a repository's default branch.

    `files` is every path the branch touches, and `authors` every author
    email on the commits it adds. Both are what the room needs to prove a
    branch is its OWN before recycling it: a branch named
    `kobo/secrets-wiring` that changes anything but `gojira.yaml`, or that
    carries a commit by a person, was made by somebody else and is not the
    room's to delete.

    `found` is False when the comparison could not be made at all (either
    ref is gone). A caller must treat that as "cannot prove it", never as
    "nothing changed": an empty `files` from a failed lookup and an empty
    `files` from an identical branch are the same value and opposite
    answers.
    """

    found: bool
    files: tuple[str, ...]
    authors: tuple[str, ...]


@dataclass(frozen=True)
class RepoAccessInfo:
    """What the room can do with a repository it does not own.

    `visible` False means the App cannot see the repository: it does not
    exist, or it exists and nobody granted the room access to it. Those two
    are genuinely indistinguishable from here (GitHub answers both with a
    404, deliberately, so a token cannot enumerate private repositories),
    and the connect scan says so in one sentence rather than guessing.
    `default_branch` and `full_name` are empty in that case, since there is
    nothing to name.

    `full_name` is GitHub's OWN spelling of `owner/name`. GitHub resolves
    both halves case-insensitively and answers with the one real spelling,
    so this is what the room records: otherwise two people connecting the
    same repository with different capitalisation would leave the room
    holding two names for one thing.
    """

    visible: bool
    can_write: bool
    default_branch: str
    full_name: str = ""


# One page of `GET /installation/repositories`. GitHub's own maximum, and
# what the repository typeahead asks for: a listing worth caching is one
# taken in as few round trips as the API allows.
INSTALLATION_REPOS_PER_PAGE = 100

# A ceiling on how far the listing will walk, in pages. An installation on
# 10,000 repositories is not a room this typeahead was written for, and a
# paging loop with no end is one bad response away from never returning.
INSTALLATION_REPOS_MAX_PAGES = 100


@dataclass(frozen=True)
class InstallationRepo:
    """One repository the room's GitHub App installation can see.

    Only the five fields the repository typeahead needs: the name to match
    and show, `archived` because an archived repository cannot be connected
    and should not be offered, `private` because a person picking from a
    list deserves to know which one they are pointing at, `default_branch`
    because the connect form carries it onward, and `pushed_at` because it
    is the only honest way to order two repositories that match a query
    equally well -- the one worked on last week is the one being looked
    for far more often than the one abandoned in 2019.

    `pushed_at` is GitHub's own ISO-8601 string, kept as text: it is only
    ever compared with another one of its own kind, and that comparison is
    the same either way round.
    """

    full_name: str
    archived: bool
    private: bool
    default_branch: str
    pushed_at: str


@runtime_checkable
class GitHubClient(Protocol):
    def repo_exists(self, repo: str) -> bool:
        """Whether `repo` (owner/name) exists."""
        ...

    def repo_access(self, repo: str) -> RepoAccessInfo:
        """What the room may do with `repo`, read from `repos/{repo}`.

        The connect scan's first question (connected-agents design
        2026-09-21), and the reason `repo_exists` is not enough for it:
        that one collapses every failure to False and can never say
        "visible but read-only". A 404 or a 403 here is `visible=False`
        rather than an exception -- "there is nothing here" and "you may
        not look at this" are the same answer for a room that can act on
        neither, and a person typing a repository name into a form gets a
        report, not a stack trace. Anything else still raises, because a
        bad token says nothing about this repository.

        `can_write` has two sources, by how the client is authenticated.
        Running as an installed App (`installation_writes` given), it is
        the installation's own `contents` grant: GitHub returns
        `permissions` on a repository read as a USER's standing and answers
        an App with every field False, `push` and `pull` alike, even on a
        repository the App created and pushes to daily. Reading `push`
        there refused every repository (example-org, 2026-09-25). Running under
        an operator's `gh auth` (dev), `push` is that person's real
        standing and is read as before; a response with no `permissions`
        object at all is still no write. A response that does not say the
        room may write is not a response that says it may: the scan's own
        message names what write access is for, so an owner reading it
        knows what to grant, whereas assuming write here would surface
        instead as a failed pull request halfway through a
        pipeline.
        """
        ...

    def read_file(self, repo: str, path: str, ref: str | None = None) -> str | None:
        """The text of one file in `repo`, at `ref` when one is named and
        the default branch otherwise, or None.

        `ref` is a branch name, and it is how a caller reads what a
        PROPOSED change contains rather than what is merged: the secrets
        wiring checks the branch behind its own open pull request before
        treating that request as already carrying the line
        (`kobo_api.agent_env`).

        None for every "there is nothing here to read": no such file, a
        path the room may not read, anything that is not a plain file (a
        directory, a symlink, a submodule -- each answers with content of
        its own that is not the file anyone asked for), a file over
        `MAX_READ_FILE_BYTES`, or one GitHub declines to inline. The
        connect scan treats an absent Dockerfile as its own finding, so a
        None that arrived for a different reason still lands somewhere a
        person can act on.

        The room's first file read that does not clone. Everything else
        here reads a working tree; the scan runs before an agent row
        exists, against a repository the room may turn out not to be
        allowed to touch, so cloning to read two small files would be both
        slower and more than it was given permission to do.
        """
        ...

    def list_root(self, repo: str) -> list[str]:
        """The names in `repo`'s root directory, files and directories
        alike, or an empty list when there is nothing there (an empty
        repository answers the contents endpoint with a 404).

        One read answers several of the scan's questions -- a Dockerfile, a
        committed `.env`, a `bundle/` directory, a `gojira.yaml` -- which is
        why it is a listing rather than four existence checks.
        """
        ...

    def list_installation_repos(self) -> list[InstallationRepo]:
        """Every repository the room's GitHub App installation can see.

        The repository typeahead's one read (`GET /api/connect/repos`). It
        names nothing the App could not already read, which is why it is
        allowed to be listed at all -- but it is a LISTING, not a lookup,
        so it is read once and cached rather than asked per keystroke.

        Raises rather than answering an empty list when GitHub will not
        answer. An installation with no repositories and a token that
        cannot ask the question are different facts, and a typeahead that
        silently shows nothing for the second is one nobody can debug.
        """
        ...

    def branch_protected(self, repo: str, branch: str) -> bool:
        """Whether `branch` is protected in `repo`.

        Not a permission check: it is how the scan warns, early, that the
        room's own wiring pull request will wait for a person rather than
        being merged by the room. A branch that is not there is not
        protected.
        """
        ...

    def create_repo_from_dir(self, repo: str, src: Path) -> None:
        """git-init a freshly-composed directory and push it as a new private repo."""
        ...

    def clone(self, repo: str, dest: Path) -> None: ...

    def commit_push(self, workdir: Path, message: str) -> bool:
        """Stage, commit, and push everything under workdir. False if nothing changed."""
        ...

    def open_pr(self, workdir: Path, *, repo: str, branch: str, title: str, body: str) -> str:
        """From a dirty clone (`workdir`, already `clone()`d and then edited
        by the caller): checkout a fresh `branch`, stage and commit
        everything under it, push it, and open a PR against `repo`'s default
        branch. Returns the PR's URL.

        Unlike `commit_push` (which stages/commits/pushes onto whatever
        branch is already checked out — used for repos kobo owns outright,
        like an agent repo), this always creates its own branch first: the
        caller here is a repo kobo does NOT own outright (bootstrap's
        skills-repo publish-workflow install, Task 7) — a PR earns the same
        review an operator would give any other change to that repo, rather
        than kobo pushing straight to its default branch.
        """
        ...

    def find_pr(self, repo: str, *, branch: str) -> str | None:
        """URL of the open PR from `branch` in `repo`, or None if there isn't
        one.

        The idempotence check `open_pr` callers need before retrying a PR
        path a second time (bootstrap's skills-publish-workflow install,
        Task 7 live-incident fix): run 1 opens a PR from a fixed branch name
        and leaves it open; run 2's `open_pr` would otherwise `git push`
        that same branch a second time and fail non-fast-forward against the
        remote branch run 1 already pushed. Checking this first lets the
        caller report "skipped" with the existing PR's URL instead.
        """
        ...

    def compare(self, repo: str, base: str, head: str) -> BranchComparison:
        """What `head` changed against `base` in `repo`: the paths it
        touches and the authors of the commits it adds.

        The one read that can tell a branch the room made from a branch
        that merely shares its name. `BranchComparison.found` is False when
        either ref is missing, which a caller must never read as "nothing
        changed" (see that class's own docstring).
        """
        ...

    def delete_branch(self, repo: str, branch: str) -> bool:
        """Delete `branch` from `repo`. True when it was deleted, False
        when it was not there, `ClientError` for anything else.

        Only ever called on a branch the ROOM created and named
        (`kobo_api.agent_env.SECRETS_WIRING_BRANCH`, `ENV_BRANCH`), and
        only when no pull request is open from it -- a merged-and-not-
        deleted branch of the room's own is the room's to recycle, and
        refusing to touch it would leave a panel permanently unable to
        save. Every other branch in a repository belongs to its team.
        """
        ...

    def branch_exists(self, repo: str, branch: str) -> bool:
        """Whether `branch` exists in `repo`.

        Deliberately NOT `repo_exists`-shaped: that one collapses every
        failure to False, which is right for "is there a repo here at all"
        and wrong here. Only a 404 means "no such branch"; anything else
        raises, because answering "no branch" to a 401 would send the
        caller into a push that then fails for a reason it believed it had
        already ruled out.
        """
        ...

    def close_pr(self, repo: str, pr_url: str, comment: str) -> None:
        """Close the pull request at `pr_url`, leaving `comment` on it
        first so nobody finds a request of the room's closed with no word
        about why.

        Only ever called on a pull request the room itself opened, and only
        when a later change in the room has made it wrong to merge: an
        unwire request still open when somebody adds a secret back would,
        merged, take the line away from an agent that needs it.

        Closing one that is already closed (or merged) is not an error:
        this is reached from a retry as easily as from a first attempt.
        """
        ...

    def merge_pr(self, repo: str, pr_url: str) -> bool:
        """Merge the pull request at `pr_url`. True when it is merged (or was
        already), False when GitHub refuses to merge it, `ClientError` for
        anything else.

        Tries each of `MERGE_METHODS` in turn: a repository may allow only
        one of them and refuses the others by name. A refusal about the
        pull request itself (rather than the method) is the answer for
        every method, so it stops there.

        The False answer is the whole point of this method having one: a
        protected branch, a required review, or a required check is an
        ORDINARY outcome for a repo the room does not own (connected-agents
        design 2026-09-21) — the caller turns it into one plain sentence
        naming the pull request, and a person merges it. Only a failure
        nobody could merge their way out of (bad credentials, an
        unreachable API) is an error.
        """
        ...

    def local_head(self, workdir: Path) -> str:
        """The just-pushed local clone's head commit sha (`git rev-parse HEAD`
        against `workdir`), not a fresh API read.

        Deliberately local rather than `head_sha(repo)`: right after
        `commit_push`, GitHub's API can lag a moment before it reflects the
        push (the same read-after-write lag `runs_for_commit` callers already
        have to tolerate) — the local clone's HEAD is the pushed sha with no
        such lag, since it's the commit this process itself just made.
        """
        ...

    def head_sha(self, repo: str) -> str:
        """The current head commit sha of `repo`'s default branch.

        Deliberately `gh api repos/<repo>/commits --jq '.[0].sha'` rather than
        `gh api repos/<repo>/commits/<branch>`: the latter needs the default
        branch's *name* first (another round trip, or a hardcoded guess like
        "main"); GitHub's commits-list endpoint already defaults to the repo's
        default branch when no ref is given, so its first entry's sha is the
        exact same value for one shell-out instead of two.
        """
        ...

    def latest_run(self, repo: str) -> dict | None:
        """The repo's most recent workflow run ({id, status, conclusion,
        head_sha}), or None when the repo has never run a workflow."""
        ...

    def run_log(self, repo: str, run_id: int, *, failed_only: bool = True) -> str:
        """One workflow run's log text -- the failed steps' logs when
        failed_only (the build-log viewer's default; whole-run logs
        otherwise). Raw text as gh renders it; the CALLER budgets/tails."""
        ...

    def runs_for_commit(self, repo: str, sha: str) -> list[dict]:
        """Workflow runs triggered by commit `sha`, newest first — each a
        {"id": int, "status": str, "conclusion": str | None} dict.

        Keyed by commit rather than "latest across the whole repo" (the old
        `latest_run`'s mistake): a run for some other commit — a regeneration
        PR's own CI, say — can never masquerade as this commit's result.
        Empty means nothing has registered against this sha *yet*, which
        callers must treat as "keep polling", not "not found".
        """
        ...

    def rerun(self, repo: str, run_id: int) -> None:
        """Re-run workflow run `run_id` (gh reuses the same id, it doesn't
        mint a new one) — lets a step self-heal a first-run auth-propagation
        failure instead of an operator having to `gh run rerun` by hand.
        """
        ...

    def run_jobs(self, repo: str, run_id: int) -> list[dict]:
        """The jobs of workflow run `run_id`, each a {"name", "status",
        "conclusion", "steps": [{"name", "status", "conclusion"}]} dict --
        names and outcomes only, in the run's own order.

        What lets a step say WHERE a run stands ("job X is still on step
        Y", "job X failed at step Y") instead of echoing the run's status:
        a run that never concludes inside the room's wait is otherwise
        reported as "in_progress", which names nothing anyone can act on.
        Empty when the run has no jobs yet.
        """
        ...

    def commit_shas_for_paths(self, workdir: Path, paths: list[str]) -> dict[str, str]:
        """Each path's last-touch commit sha (`git log -1 --format=%H --
        <path>`, run once per path) inside an already-cloned `workdir`.

        Used by `kobo_api.intake.list_intake` to find, for every
        `bundle/intake/*` file, the commit that last modified it — the
        input `is_ancestor` compares against the live Deployment's commit to
        derive that file's pending/ingested status.
        """
        ...

    def is_ancestor(self, workdir: Path, ancestor: str, descendant: str) -> bool:
        """Whether `ancestor` is `descendant` itself or reachable from it
        (`git merge-base --is-ancestor ancestor descendant`) inside
        `workdir`'s history.

        Equality counts as ancestry here — matches git's own semantics for
        this flag — which is exactly the shape `list_intake` relies on: a
        file whose last-touch commit IS the live commit is exactly as
        "ingested" as one genuinely older.
        """
        ...

    def rev_parse_prefix(self, workdir: Path, prefix: str) -> str | None:
        """Resolve `prefix` to a full commit sha inside `workdir`'s history,
        or `None` if it doesn't resolve to anything — never raises for
        unresolvable input.

        `list_intake` calls this with the live image's 12-hex commit-prefix
        tag; an image built from a since-force-pushed sha (or any other
        malformed/non-hex tag, e.g. "latest") must read as "can't prove
        ingested" (pending), not crash the whole listing.
        """
        ...

    def rename_repo(self, repo: str, new_name: str) -> None:
        """Rename `repo` (owner/name) to `new_name`, same owner (Task 3:
        agent destruction's repo-archive step renames before archiving, so
        the `agent-<name>` slug is free for a future agent).

        Raises `ClientError` on failure; a 404 (the repo doesn't exist —
        already renamed by a prior partial run, or never existed) raises a
        `ClientError` whose message starts with `"repo not found: "` so
        `kobo_api.destroy`'s step logic can branch on it as an
        already-gone signal without string-matching gh's own wording.
        """
        ...

    def archive_repo(self, repo: str) -> None:
        """Archive `repo` (owner/name) — the terminal, read-only state a
        destroyed agent's repo ends in (never deleted; see the destruction
        design's "what survives" inventory).

        Raises `ClientError` on failure; a 404 raises a `ClientError`
        whose message starts with `"repo not found: "`, same convention as
        `rename_repo`. An already-archived repo's PATCH 403s rather than
        404s (GitHub's own "read-only" response) — recognizing THAT shape
        is the destroy step's job (Task 4), not this transport method's;
        it just raises the plain `ClientError` for any non-404 failure.
        """
        ...


def _gh_returned_404(exc: ClientError) -> bool:
    """Whether a `ClientError` raised from a `gh api` call reflects a 404
    from GitHub's REST API.

    `gh` formats any non-2xx API response as `gh: <message> (HTTP
    <code>)` on stderr — confirmed live against a real, authenticated `gh`
    (`gh api -X PATCH repos/<a-repo-that-does-not-exist>`: exit 1, stdout
    the API's own `{"message":"Not Found",...,"status":"404"}` body,
    stderr exactly `gh: Not Found (HTTP 404)`). Matching on the `(HTTP
    404)` status marker rather than the free-text message is deliberate:
    GitHub's API is free to reword "Not Found", but gh's own status-code
    formatting is stable.
    """
    return "(HTTP 404)" in exc.stderr


def _gh_returned_403(exc: ClientError) -> bool:
    """Whether a `ClientError` from a `gh api` call reflects a 403.

    Same stable-marker matching as `_gh_returned_404` above, and read by
    the same callers for one reason: GitHub answers "you may not look at
    this" with a 403 about as readily as it answers "there is nothing
    here" with a 404, and a reader that classified only one of them would
    turn the other into a 500 in front of somebody filling in a form.

    Deliberately NOT used by the write paths (`rename_repo`,
    `archive_repo`): a 403 there is `destroy._is_already_archived`'s to
    read, and it means something quite different.
    """
    return "(HTTP 403)" in exc.stderr


# The merge methods `merge_pr` will ask for, in order. A repository may
# allow only one of the three, and refuses the others by name -- the
# operator ruling is that the room merges by default, so it asks the ways
# the repository might accept rather than giving up on the first no.
MERGE_METHODS = ("--merge", "--squash", "--rebase")

# Fragments of what `gh pr merge` prints when GitHub declines to merge, as
# opposed to failing. Matched case-insensitively as substrings of gh's
# STDERR ONLY -- never of the ClientError's message, which embeds the argv
# and the pull request URL, so a branch or repository whose name contains
# one of these words would otherwise classify itself. Same "classify by a
# stable fragment, never by exact text" discipline `_gh_returned_404` above
# and `destroy._is_already_archived` already hold: GitHub is free to reword
# these sentences, and a merge refusal arrives as a GraphQL message rather
# than an HTTP status marker, so there is no code to key on the way there
# is for a 404.
_MERGE_REFUSED_FRAGMENTS = (
    "not mergeable",
    "base branch policy",
    "required status check",
    "review is required",
    "approving review is required",
    "protected branch",
    # A repository that merges through a queue does not accept a direct
    # merge from anyone, the room included. Not an error: a person moves it
    # through the queue.
    "merge queue",
)

# How GitHub says "this repository does not do it that way" -- about the
# METHOD, not about this pull request. The only refusal worth answering by
# asking again with a different method.
_MERGE_METHOD_REFUSED_FRAGMENTS = (
    "are not allowed on this repository",
    "merge method is not allowed",
)

# What `gh pr merge` says when somebody merged the pull request first, in
# both of the phrasings seen (gh's own one-liner and GitHub's GraphQL
# message). That is the outcome the caller wanted, so it reads as success.
_MERGE_ALREADY_DONE_FRAGMENTS = ("already merged", "already been merged")


def _gh_declined_the_merge(exc: ClientError) -> bool:
    """Whether `exc` is GitHub declining to merge rather than failing."""
    stderr = exc.stderr.lower()
    return any(fragment in stderr for fragment in _MERGE_REFUSED_FRAGMENTS)


def _gh_refused_the_merge_method(exc: ClientError) -> bool:
    """Whether `exc` is about the merge METHOD rather than this pull request."""
    stderr = exc.stderr.lower()
    return any(fragment in stderr for fragment in _MERGE_METHOD_REFUSED_FRAGMENTS)


def _gh_says_already_merged(exc: ClientError) -> bool:
    stderr = exc.stderr.lower()
    return any(fragment in stderr for fragment in _MERGE_ALREADY_DONE_FRAGMENTS)


class GhCLI:
    """git + gh backed GitHubClient.

    Two modes, chosen by `token_provider`: with none (the default), git/gh
    run under the operator's own ambient `gh auth` session, unchanged —
    dev's only mode today. With one set (deployed, Task 3: `Clients.real()`
    wires an `InstallationTokenProvider` there iff the App config is fully
    set), every invocation below passes `env={"GH_TOKEN": token_provider()}`
    — `gh` reads GH_TOKEN directly; `git` picks it up via the credential
    helper the deployed container bakes in (`gh auth git-credential`,
    Task 4's job, not this class's). Called fresh per invocation rather than
    cached here — `InstallationTokenProvider.token()` already caches, so
    this always gets a live token without this class needing to know
    anything about its expiry.

    Mirrors gojira's GhCLI (gojira/github.py): auth-free local git plumbing
    plus `gh` for anything that needs the GitHub API.
    """

    def __init__(
        self,
        runner: CommandRunner | None = None,
        git: str = "git",
        gh: str = "gh",
        token_provider: Callable[[], str] | None = None,
        installation_writes: Callable[[], bool] | None = None,
    ):
        self._runner = runner or CommandRunner()
        self._git = git
        self._gh = gh
        self._token_provider = token_provider
        # Set alongside an App `token_provider`: whether the installation
        # may write contents (`InstallationTokenProvider.can_write_contents`).
        # `repo_access` reads it instead of GitHub's per-repository
        # `permissions`, which are a user's standing and False for an App.
        self._installation_writes = installation_writes

    def _env(self) -> dict[str, str] | None:
        if self._token_provider is None:
            return None
        return {"GH_TOKEN": self._token_provider()}

    def repo_exists(self, repo: str) -> bool:
        try:
            self._runner.run([self._gh, "repo", "view", repo], env=self._env())
            return True
        except ClientError:
            return False

    def create_repo_from_dir(self, repo: str, src: Path) -> None:
        self._runner.run([self._git, "-C", str(src), "init"], env=self._env())
        self._runner.run([self._git, "-C", str(src), "branch", "-M", "main"], env=self._env())
        self._runner.run([self._git, "-C", str(src), "add", "-A"], env=self._env())
        self._runner.run(
            [
                self._git, "-C", str(src),
                "-c", f"user.name={COMMIT_AUTHOR_NAME}",
                "-c", f"user.email={COMMIT_AUTHOR_EMAIL}",
                "commit", "-m", "Initial commit",
            ],
            env=self._env(),
        )
        self._runner.run(
            [self._gh, "repo", "create", repo, "--private", "--source", str(src), "--push"],
            env=self._env(),
        )

    def clone(self, repo: str, dest: Path) -> None:
        self._runner.run([self._gh, "repo", "clone", repo, str(dest)], env=self._env())

    def commit_push(self, workdir: Path, message: str) -> bool:
        self._runner.run([self._git, "-C", str(workdir), "add", "-A"], env=self._env())
        status = self._runner.run(
            [self._git, "-C", str(workdir), "status", "--porcelain"], env=self._env()
        )
        if not status:
            log.info("nothing to commit in %s", workdir)
            return False
        self._runner.run(
            [
                self._git, "-C", str(workdir),
                "-c", f"user.name={COMMIT_AUTHOR_NAME}",
                "-c", f"user.email={COMMIT_AUTHOR_EMAIL}",
                "commit", "-m", message,
            ],
            env=self._env(),
        )
        self._runner.run([self._git, "-C", str(workdir), "push"], env=self._env())
        return True

    def open_pr(self, workdir: Path, *, repo: str, branch: str, title: str, body: str) -> str:
        self._runner.run(
            [self._git, "-C", str(workdir), "checkout", "-b", branch], env=self._env()
        )
        self._runner.run([self._git, "-C", str(workdir), "add", "-A"], env=self._env())
        self._runner.run(
            [
                self._git, "-C", str(workdir),
                "-c", f"user.name={COMMIT_AUTHOR_NAME}",
                "-c", f"user.email={COMMIT_AUTHOR_EMAIL}",
                "commit", "-m", title,
            ],
            env=self._env(),
        )
        self._runner.run(
            [self._git, "-C", str(workdir), "push", "-u", "origin", branch], env=self._env()
        )
        return self._runner.run(
            [
                self._gh, "pr", "create",
                "--repo", repo, "--title", title, "--body", body, "--head", branch,
            ],
            env=self._env(),
        )

    def find_pr(self, repo: str, *, branch: str) -> str | None:
        out = self._runner.run(
            [
                self._gh, "pr", "list",
                "--repo", repo, "--head", branch, "--state", "open",
                "--json", "url", "--jq", ".[0].url",
            ],
            env=self._env(),
        )
        return out if out else None

    def branch_exists(self, repo: str, branch: str) -> bool:
        try:
            self._runner.run(
                [self._gh, "api", f"repos/{repo}/branches/{branch}"], env=self._env()
            )
        except ClientError as exc:
            if _gh_returned_404(exc):
                return False
            raise
        return True

    def compare(self, repo: str, base: str, head: str) -> BranchComparison:
        try:
            out = self._runner.run(
                [
                    self._gh, "api",
                    # Both refs are one path segment each here, so a slash
                    # in a branch name has to be encoded -- unlike the git
                    # refs route `delete_branch` below uses.
                    f"repos/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}",
                ],
                env=self._env(),
            )
        except ClientError as exc:
            if _gh_returned_404(exc):
                return BranchComparison(found=False, files=(), authors=())
            raise
        payload = json.loads(out) if out else {}
        if not isinstance(payload, dict):
            return BranchComparison(found=False, files=(), authors=())
        files = tuple(
            str(entry.get("filename", ""))
            for entry in payload.get("files") or []
            if isinstance(entry, dict)
        )
        authors = []
        for commit in payload.get("commits") or []:
            if not isinstance(commit, dict):
                continue
            # The git author recorded IN the commit, not GitHub's guess at
            # which account it belongs to: the room commits under its own
            # name and email, and that is the thing to match.
            detail = commit.get("commit") or {}
            author = detail.get("author") if isinstance(detail, dict) else None
            if isinstance(author, dict):
                authors.append(str(author.get("email", "")))
        return BranchComparison(found=True, files=files, authors=tuple(authors))

    def delete_branch(self, repo: str, branch: str) -> bool:
        try:
            self._runner.run(
                [
                    self._gh, "api", "-X", "DELETE",
                    # The branch name goes in RAW, unlike `branch_protected`
                    # below. That route takes the name as one path segment,
                    # so a slash in it has to be encoded; this one takes a
                    # git REF, where `heads/kobo/secrets-wiring` is the
                    # whole thing and the slashes are part of the path.
                    # Encoding them asked GitHub to delete a ref literally
                    # named `heads/kobo%2Fsecrets-wiring`, which is a 404,
                    # which this method then reported as "already gone".
                    f"repos/{repo}/git/refs/heads/{branch}",
                ],
                env=self._env(),
            )
        except ClientError as exc:
            if _gh_returned_404(exc):
                # Already gone, which is what the caller wanted.
                return False
            raise
        return True

    def repo_access(self, repo: str) -> RepoAccessInfo:
        try:
            out = self._runner.run([self._gh, "api", f"repos/{repo}"], env=self._env())
        except ClientError as exc:
            if _gh_returned_404(exc) or _gh_returned_403(exc):
                return RepoAccessInfo(visible=False, can_write=False, default_branch="")
            raise
        data = json.loads(out) if out else {}
        # `or {}` as well as the default: GitHub can answer with a null
        # `permissions` as easily as with no key at all, and both mean the
        # same thing here (see the Protocol's docstring for why that reads
        # as no write).
        permissions = data.get("permissions") or {}
        if self._installation_writes is not None:
            # An App: the repository is visible (200 above) and the grant
            # is the installation's, so the grant is the answer.
            can_write = self._installation_writes()
        else:
            can_write = bool(permissions.get("push", False))
        return RepoAccessInfo(
            visible=True,
            can_write=can_write,
            default_branch=data.get("default_branch") or "",
            full_name=data.get("full_name") or "",
        )

    def read_file(self, repo: str, path: str, ref: str | None = None) -> str | None:
        endpoint = f"repos/{repo}/contents/{path}"
        if ref:
            endpoint = f"{endpoint}?ref={quote(ref, safe='')}"
        try:
            out = self._runner.run(
                [self._gh, "api", endpoint], env=self._env()
            )
        except ClientError as exc:
            if _gh_returned_404(exc) or _gh_returned_403(exc):
                return None
            raise
        payload = json.loads(out) if out else None
        # A directory comes back as a JSON array. The caller asked for a
        # file, so this is "nothing to read", not an error.
        if not isinstance(payload, dict):
            return None
        # A symlink and a submodule each answer with content of their own
        # (a target path, a commit sha) that is not the file anyone asked
        # for, so only a plain file reads as one.
        if payload.get("type") != "file":
            return None
        if payload.get("size", 0) > MAX_READ_FILE_BYTES:
            return None
        # GitHub answers a large file with an empty body and encoding
        # "none" rather than refusing, so the encoding is checked before
        # the content is trusted to be base64 at all.
        if payload.get("encoding") != "base64":
            return None
        raw = base64.b64decode(payload.get("content") or "")
        if len(raw) > MAX_READ_FILE_BYTES:
            return None
        # Never raises on a file that is not text: the caller is matching
        # lines in a Dockerfile, and one undecodable byte is not a reason
        # to fail a scan.
        return raw.decode("utf-8", errors="replace")

    def list_root(self, repo: str) -> list[str]:
        try:
            out = self._runner.run(
                [self._gh, "api", f"repos/{repo}/contents/", "--jq", ".[].name"],
                env=self._env(),
            )
        except ClientError as exc:
            if _gh_returned_404(exc) or _gh_returned_403(exc):
                return []
            raise
        return [line for line in out.splitlines() if line]

    def list_installation_repos(self) -> list[InstallationRepo]:
        """Walks `installation/repositories` a page at a time, stopping at
        the first page that came back short of the page size.

        `page=` rather than `Link` headers: `gh api` hands back the body,
        not the response headers, and a short page is the same end of the
        listing GitHub's own `Link` would have described. Bounded by
        `INSTALLATION_REPOS_MAX_PAGES` so a server that keeps answering
        full pages cannot hold this open forever.
        """
        repos: list[InstallationRepo] = []
        for page in range(1, INSTALLATION_REPOS_MAX_PAGES + 1):
            path = (
                "installation/repositories"
                f"?per_page={INSTALLATION_REPOS_PER_PAGE}&page={page}"
            )
            out = self._runner.run([self._gh, "api", path], env=self._env())
            payload = json.loads(out) if out else {}
            entries = payload.get("repositories") or []
            for entry in entries:
                full_name = entry.get("full_name") or ""
                if not full_name:
                    continue
                repos.append(
                    InstallationRepo(
                        full_name=full_name,
                        archived=bool(entry.get("archived", False)),
                        private=bool(entry.get("private", False)),
                        default_branch=entry.get("default_branch") or "",
                        pushed_at=entry.get("pushed_at") or "",
                    )
                )
            if len(entries) < INSTALLATION_REPOS_PER_PAGE:
                break
        else:
            log.warning(
                "installation repository listing stopped at the %s-page cap",
                INSTALLATION_REPOS_MAX_PAGES,
            )
        return repos

    def branch_protected(self, repo: str, branch: str) -> bool:
        try:
            out = self._runner.run(
                [
                    self._gh, "api",
                    # A branch name is ONE path segment and may hold
                    # slashes of its own (`release/main`), which unencoded
                    # would ask GitHub about a different branch entirely.
                    f"repos/{repo}/branches/{quote(branch, safe='')}",
                    "--jq", ".protected",
                ],
                env=self._env(),
            )
        except ClientError as exc:
            if _gh_returned_404(exc) or _gh_returned_403(exc):
                return False
            raise
        return out.strip() == "true"

    def close_pr(self, repo: str, pr_url: str, comment: str) -> None:
        # The comment first, so a closed request always carries the reason
        # even if the close itself then fails and is retried.
        self._runner.run(
            [self._gh, "pr", "comment", pr_url, "--repo", repo, "--body", comment],
            env=self._env(),
        )
        try:
            self._runner.run(
                [self._gh, "pr", "close", pr_url, "--repo", repo], env=self._env()
            )
        except ClientError as exc:
            if _gh_says_already_merged(exc) or _gh_returned_404(exc):
                # Already closed, already merged, or gone. Each is the
                # state this call wanted; a retry must not fail on one.
                return
            raise

    def merge_pr(self, repo: str, pr_url: str) -> bool:
        for method in MERGE_METHODS:
            try:
                self._runner.run(
                    [self._gh, "pr", "merge", pr_url, method], env=self._env()
                )
            except ClientError as exc:
                if _gh_says_already_merged(exc):
                    return True
                if _gh_refused_the_merge_method(exc):
                    # About the method, not this pull request: the next one
                    # may be the one this repository allows.
                    log.info(
                        "github does not allow %s in %s; trying the next method",
                        method, repo,
                    )
                    continue
                if _gh_declined_the_merge(exc):
                    # Logged at info, not warning: this is the owner's own repo
                    # policy working as intended, and the caller already turns it
                    # into something a person can act on. Refused on the pull
                    # request's own merits, so another method would be refused
                    # the same way -- stop asking.
                    log.info("github declined to merge %s in %s", pr_url, repo)
                    return False
                raise
            else:
                return True

        log.info("github allows none of %s in %s", ", ".join(MERGE_METHODS), repo)
        return False

    def local_head(self, workdir: Path) -> str:
        return self._runner.run(
            [self._git, "-C", str(workdir), "rev-parse", "HEAD"], env=self._env()
        )

    def head_sha(self, repo: str) -> str:
        return self._runner.run(
            [self._gh, "api", f"repos/{repo}/commits", "--jq", ".[0].sha"], env=self._env()
        )

    def runs_for_commit(self, repo: str, sha: str) -> list[dict]:
        out = self._runner.run(
            [
                self._gh, "run", "list", "--repo", repo, "--commit", sha,
                "--json", "databaseId,status,conclusion",
            ],
            env=self._env(),
        )
        runs = json.loads(out) if out else []
        return [
            {
                "id": run["databaseId"],
                "status": run["status"],
                "conclusion": run.get("conclusion"),
            }
            for run in runs
        ]

    def latest_run(self, repo: str) -> dict | None:
        out = self._runner.run(
            [
                self._gh, "run", "list", "--repo", repo, "-L", "1",
                "--json", "databaseId,status,conclusion,headSha",
            ],
            env=self._env(),
        )
        runs = json.loads(out) if out else []
        if not runs:
            return None
        run = runs[0]
        return {
            "id": run["databaseId"],
            "status": run["status"],
            "conclusion": run.get("conclusion"),
            "head_sha": run.get("headSha", ""),
        }

    def run_log(self, repo: str, run_id: int, *, failed_only: bool = True) -> str:
        flag = "--log-failed" if failed_only else "--log"
        return self._runner.run(
            [self._gh, "run", "view", str(run_id), "--repo", repo, flag],
            env=self._env(),
        )

    def rerun(self, repo: str, run_id: int) -> None:
        self._runner.run(
            [self._gh, "run", "rerun", str(run_id), "--repo", repo], env=self._env()
        )

    def run_jobs(self, repo: str, run_id: int) -> list[dict]:
        out = self._runner.run(
            [self._gh, "run", "view", str(run_id), "--repo", repo, "--json", "jobs"],
            env=self._env(),
        )
        payload = json.loads(out) if out else {}
        return [
            {
                "name": job.get("name", ""),
                "status": job.get("status", ""),
                "conclusion": job.get("conclusion") or None,
                "steps": [
                    {
                        "name": step.get("name", ""),
                        "status": step.get("status", ""),
                        "conclusion": step.get("conclusion") or None,
                    }
                    for step in job.get("steps") or []
                ],
            }
            for job in payload.get("jobs") or []
        ]

    def commit_shas_for_paths(self, workdir: Path, paths: list[str]) -> dict[str, str]:
        return {
            path: self._runner.run(
                [self._git, "-C", str(workdir), "log", "-1", "--format=%H", "--", path],
                env=self._env(),
            )
            for path in paths
        }

    def is_ancestor(self, workdir: Path, ancestor: str, descendant: str) -> bool:
        try:
            self._runner.run(
                [
                    self._git, "-C", str(workdir),
                    "merge-base", "--is-ancestor", ancestor, descendant,
                ],
                env=self._env(),
            )
            return True
        except ClientError as exc:
            if exc.returncode == 1:
                return False
            raise

    def rev_parse_prefix(self, workdir: Path, prefix: str) -> str | None:
        try:
            return self._runner.run(
                [self._git, "-C", str(workdir), "rev-parse", f"{prefix}^{{commit}}"],
                env=self._env(),
            )
        except ClientError:
            return None

    def rename_repo(self, repo: str, new_name: str) -> None:
        try:
            self._runner.run(
                [self._gh, "api", "-X", "PATCH", f"repos/{repo}", "-f", f"name={new_name}"],
                env=self._env(),
            )
        except ClientError as exc:
            if _gh_returned_404(exc):
                raise ClientError(f"repo not found: {repo}") from exc
            raise

    def archive_repo(self, repo: str) -> None:
        try:
            self._runner.run(
                [self._gh, "api", "-X", "PATCH", f"repos/{repo}", "-F", "archived=true"],
                env=self._env(),
            )
        except ClientError as exc:
            if _gh_returned_404(exc):
                raise ClientError(f"repo not found: {repo}") from exc
            raise


# --- Gojira ---------------------------------------------------------------------


@runtime_checkable
class GojiraClient(Protocol):
    def create_app(
        self,
        *,
        name: str,
        repo_full_name: str,
        cluster: str,
        namespace: str,
        workload_class: str = "agent",
        auto_merge_setup_pr: bool = True,
    ) -> int:
        """POST /api/v1/apps; returns the new app id. Raises GojiraConflict on 409.

        `workload_class` chooses how the platform runs and probes the
        workload. It defaults to the one every composed agent has always
        sent; a connected agent that serves no HTTP port asks for whichever
        class this deployment names for that (connected-agents design
        2026-09-21), since the ordinary class probes a port over HTTP.

        `auto_merge_setup_pr` is the room waiving review of the platform's
        setup pull request. That waiver belongs only to a repository the
        room itself authored, where there is no human content to protect;
        a connected repository is a team's own, and its owner merges the
        wiring (Ember's review, 2026-09-29). The platform refuses the flag
        for a repository it recognises as hand-built anyway, but the room
        should not ask for what it must not have.
        """
        ...

    def app_status(self, app_id: int) -> dict:
        """GET /api/v1/apps/{id}."""
        ...

    def reprovision(self, app_id: int) -> None:
        """POST /apps/{id}/provision to re-run the provisioner."""
        ...

    def ping(self) -> bool:
        """GET {base}/api/v1/apps (auth'd); True iff 200.

        Read-only reachability probe for the org status route's
        deploy-platform check (Task 8) — deliberately never raises: any
        transport failure (unreachable host, timeout, ...) or non-200
        response is just False, the same way every other check on that
        route reports "not reachable" rather than a 500.
        """
        ...

    def delete_app(self, app_id: int, *, delete_namespaces: bool = True) -> dict:
        """DELETE /api/v1/apps/{app_id} (Task 3: agent destruction's
        platform-teardown step); bearer via `_headers()`, same as every
        other call here.

        202 -> gojira's teardown summary dict, passed straight through.
        404 -> `{"already_gone": True}` — gojira's own idempotency
        contract (a repeat delete, or one against an app id the room never
        finished creating) is "unknown app id is not an error"; kobo's
        destroy step treats this the same as a genuine 202 teardown. Any
        other status raises `ClientError`.
        """
        ...


class HttpxGojiraClient:
    """Talks to gojira's onboarding API over httpx."""

    def __init__(
        self, base_url: str, *, token: str | None = None, client: httpx.Client | None = None
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        # gojira's onboarding calls were fast in P0 (no model roundtrip
        # involved); this default just gives headroom over httpx's bare 5s.
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))

    def _headers(self) -> dict[str, str]:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def create_app(
        self,
        *,
        name,
        repo_full_name,
        cluster,
        namespace,
        workload_class="agent",
        auto_merge_setup_pr=True,
    ) -> int:
        resp = self._client.post(
            f"{self._base_url}/api/v1/apps",
            json={
                "name": name,
                "repo_full_name": repo_full_name,
                "workload_class": workload_class,
                "environments": [{"name": "dev", "cluster": cluster, "namespace": namespace}],
                "auto_merge_setup_pr": auto_merge_setup_pr,
            },
            headers=self._headers(),
        )
        if resp.status_code == 409:
            raise GojiraConflict(f"app already connected for {repo_full_name!r}")
        if resp.status_code != 201:
            raise ClientError(f"create_app failed ({resp.status_code}): {resp.text}")
        return resp.json()["id"]

    def app_status(self, app_id: int) -> dict:
        resp = self._client.get(
            f"{self._base_url}/api/v1/apps/{app_id}", headers=self._headers()
        )
        if resp.status_code != 200:
            raise ClientError(f"app_status({app_id}) failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def reprovision(self, app_id: int) -> None:
        # Rides /api/v1 (not the UI's form route, which sits behind gojira's
        # IAP-identity gate that no server-to-server caller can satisfy).
        resp = self._client.post(
            f"{self._base_url}/api/v1/apps/{app_id}/provision",
            headers=self._headers(),
        )
        if resp.status_code != 202:
            raise ClientError(f"reprovision({app_id}) failed ({resp.status_code}): {resp.text}")

    def ping(self) -> bool:
        # Health check probe to GET {base}/healthz — a generic reachability
        # check that doesn't require auth, only success (200) or failure (any
        # other status/exception reads as "not reachable right now"). The deploy
        # platform's apps endpoint is POST-only (405 on GET), so this health
        # endpoint is the reliable check for org-status visibility.
        try:
            resp = self._client.get(f"{self._base_url}/healthz")
        except Exception:
            log.debug("gojira ping failed", exc_info=True)
            return False
        return resp.status_code == 200

    def delete_app(self, app_id: int, *, delete_namespaces: bool = True) -> dict:
        # httpx.Client.delete() deliberately has no json=/content= parameter
        # (a DELETE conventionally carries no body) — .request() is the seam
        # that lets this DELETE carry the delete_namespaces flag as JSON.
        resp = self._client.request(
            "DELETE",
            f"{self._base_url}/api/v1/apps/{app_id}",
            json={"delete_namespaces": delete_namespaces},
            headers=self._headers(),
            # Final-review Finding A: teardown is slow by nature (gcloud/
            # kubectl work on gojira's side, realistically 15-60s); the
            # client's default 30s timeout would manufacture a spurious
            # first-run failure on a slow but healthy teardown. The step
            # ledger already tolerates a retry, it should not have to
            # invent one.
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        if resp.status_code == 404:
            # Only gojira's own "unknown app id" 404 — body {"error": "not
            # found"}, per the platform's api_delete_app route — means
            # already-gone; a route-missing framework default or a
            # proxy/gateway 404 in between must not read as teardown done.
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("error") == "not found":
                return {"already_gone": True}
        if resp.status_code != 202:
            raise ClientError(f"delete_app({app_id}) failed ({resp.status_code}): {resp.text}")
        return resp.json()


# --- Cloud (GCP) ------------------------------------------------------------------


@runtime_checkable
class CloudClient(Protocol):
    def ensure_gsa(self, name: str) -> str:
        """describe-then-create a GSA named `name`; returns its email."""
        ...

    def bind_workload_identity(self, gsa_email: str, namespace: str, ksa: str) -> None: ...

    def add_to_group(self, gsa_email: str, group: str) -> None:
        """Add gsa_email to group; tolerates it already being a member."""
        ...

    def remove_from_group(self, gsa_email: str, group: str) -> bool:
        """Remove gsa_email from group. False if it was already absent."""
        ...

    def grant_model_access(self, gsa_email: str, project: str) -> None:
        """Grant gsa_email the Vertex AI user role on project directly — the
        road taken when the room has no model group (a project with no
        Workspace has no groups to join). Idempotent."""
        ...

    def revoke_model_access(self, gsa_email: str, project: str) -> bool:
        """Remove that grant. False if it was already absent."""
        ...

    def delete_gsa(self, email: str) -> bool:
        """Delete the GSA at email. False if it was already gone."""
        ...

    def gsa_email_for(self, name: str) -> str:
        """The email a GSA named `name` would have (or already has) in this
        client's project - the same composition `ensure_gsa` uses
        internally, exposed so a caller that needs the email (destroy-access
        teardown, kobo_api.destroy) can derive it without creating anything.
        """
        ...

    def grant_bucket_prefix_write(self, gsa_email: str, bucket: str, prefix: str) -> None:
        """Grant `gsa_email` write access to objects under `prefix` in
        `bucket` -- and nothing wider -- for agent memory replication
        (resilience arc, Task 14). Scoped by an IAM condition on the object
        name prefix, never a bucket-wide role for READ/WRITE: one agent's
        replicated techne database must never be reachable by another
        agent's own identity, even though every agent replicates into the
        same bucket.

        Important 9 fix: ALSO grants a second, unconditioned, bucket-wide,
        list-only binding (`roles/storage.legacyBucketReader`) -- `storage.
        objects.list` cannot be scoped by the same per-prefix condition
        (it is a bucket-level permission with no per-object `resource.name`
        to test), and litestream needs it to list its replica prefix's
        existing generations on startup. Room-wide for list only, never
        for read/write: no identity gains access to another agent's
        replicated data through this second grant beyond enumerating
        object names.
        """
        ...


# What a model group confers at Acme through membership; granted directly on
# the Vertex project when the room runs with no group (2026-10-01).
MODEL_ACCESS_ROLE = "roles/aiplatform.user"


def gsa_email(name: str, project: str) -> str:
    """The GSA email convention for a GCP service account: `{name}@{project}.iam.gserviceaccount.com`.

    Prefix-agnostic: this is only the email-format helper, not the naming
    policy — callers that need an agent's GSA build the full `agent-<name>`
    id themselves before calling this (both `GcloudClient.ensure_gsa` below
    and `WireStep`, `kobo_api/steps.py`, which needs the same email for the
    KSA's Workload Identity annotation), so the real client and `FakeCloud`
    (`tests/fakes.py`) now behave identically for any name it's given.
    """
    return f"{name}@{project}.iam.gserviceaccount.com"


class GcloudClient:
    """gcloud-backed CloudClient, scoped to one GCP project."""

    def __init__(self, project: str, runner: CommandRunner | None = None, gcloud: str = "gcloud"):
        self._project = project
        self._runner = runner or CommandRunner()
        self._gcloud = gcloud

    def ensure_gsa(self, name: str) -> str:
        # `name` is always the caller's full "agent-<agent name>" SA id (the
        # only caller, AccessStep, constructs it that way); gsa_email() is
        # prefix-agnostic, so it's used as-is rather than round-tripped
        # through a stripped-then-reattached prefix.
        email = gsa_email(name, self._project)
        try:
            self._runner.run(
                [
                    self._gcloud, "iam", "service-accounts", "describe", email,
                    "--project", self._project,
                ]
            )
            return email
        except ClientError as exc:
            # Only a genuine "no such GSA" describe failure falls through to
            # create — anything else (permission denied, a transient API
            # error, ...) is a real problem the caller needs to see, not
            # something a blind create-on-any-failure should paper over.
            stderr = exc.stderr.lower()
            if "not_found" not in stderr and "does not exist" not in stderr:
                raise
        self._runner.run(
            [
                self._gcloud, "iam", "service-accounts", "create", name,
                "--project", self._project, "--display-name", name,
            ]
        )
        return email

    def bind_workload_identity(self, gsa_email: str, namespace: str, ksa: str) -> None:
        member = f"serviceAccount:{self._project}.svc.id.goog[{namespace}/{ksa}]"
        self._runner.run(
            [
                self._gcloud, "iam", "service-accounts", "add-iam-policy-binding", gsa_email,
                "--project", self._project,
                "--role", "roles/iam.workloadIdentityUser",
                "--member", member,
            ]
        )

    def add_to_group(self, gsa_email: str, group: str) -> None:
        try:
            self._runner.run(
                [
                    self._gcloud, "identity", "groups", "memberships", "add",
                    "--group-email", group, "--member-email", gsa_email,
                ]
            )
        except ClientError as exc:
            stderr = exc.stderr.lower()
            if "already exists" in stderr or "already a member" in stderr:
                return
            raise

    def remove_from_group(self, gsa_email: str, group: str) -> bool:
        # Not-found grounding (read from the installed google-cloud-sdk's
        # Python source, ~/Workspace/google-cloud-sdk/lib/googlecloudsdk/
        # command_lib/identity/groups/memberships/hooks.py): before the
        # actual delete request, `gcloud identity groups memberships
        # delete` resolves --group-email/--member-email into a membership
        # resource name (SetMembershipResourceName ->
        # ConvertEmailToMembershipResourceName). That helper's lookup call
        # (LookupMembershipName) raising HttpNotFoundError for an absent
        # membership (or HttpForbiddenError -- the SDK collapses both) is
        # caught there and re-raised as a calliope InvalidArgumentException
        # with a fixed message: "There is no such membership associated
        # with the specified arguments: <group-email>, <member-email>".
        # Calliope renders any known exception as "ERROR: (<command.path>)
        # <message>" (calliope/exceptions.py, _LogKnownError), so that
        # fixed phrase is a stable substring of the real stderr. Only that
        # exact phrase is treated as absent; a real permission problem
        # surfacing from the delete request itself (post-lookup) carries
        # no such text, so it still raises.
        try:
            self._runner.run(
                [
                    self._gcloud, "identity", "groups", "memberships", "delete",
                    "--group-email", group, "--member-email", gsa_email, "--quiet",
                ]
            )
        except ClientError as exc:
            if "there is no such membership" in exc.stderr.lower():
                return False
            raise
        return True

    def grant_model_access(self, gsa_email: str, project: str) -> None:
        # add-iam-policy-binding is idempotent: a binding already there is
        # reported, not refused. --condition=None keeps gcloud from asking.
        self._runner.run(
            [
                self._gcloud, "projects", "add-iam-policy-binding", project,
                "--member", f"serviceAccount:{gsa_email}",
                "--role", MODEL_ACCESS_ROLE, "--condition=None",
            ]
        )

    def revoke_model_access(self, gsa_email: str, project: str) -> bool:
        try:
            self._runner.run(
                [
                    self._gcloud, "projects", "remove-iam-policy-binding", project,
                    "--member", f"serviceAccount:{gsa_email}",
                    "--role", MODEL_ACCESS_ROLE, "--condition=None",
                ]
            )
        except ClientError as exc:
            if "not found" in exc.stderr.lower():  # gcloud: "Policy binding ... not found!"
                return False
            raise
        return True

    def delete_gsa(self, email: str) -> bool:
        # Not-found grounding: the installed gcloud SDK's own docstring for
        # this command (~/Workspace/google-cloud-sdk/lib/surface/iam/
        # service_accounts/delete.py, class Delete) says outright: "If the
        # service account does not exist, this command returns a
        # `PERMISSION_DENIED` error." That means delete's own failure
        # can't be told apart from a real permission problem by stderr
        # text alone -- unlike describe, whose not-found rendering
        # ensure_gsa above already discriminates ("not_found"/"does not
        # exist"). So this describes first, reusing that same
        # already-grounded check, and only issues the delete once
        # describe confirms the GSA still exists -- the identical
        # describe-then-delete shape gojira's delete_service_account
        # (gojira/gcp.py) uses, for the same documented reason.
        try:
            self._runner.run(
                [
                    self._gcloud, "iam", "service-accounts", "describe", email,
                    "--project", self._project,
                ]
            )
        except ClientError as exc:
            stderr = exc.stderr.lower()
            if "not_found" not in stderr and "does not exist" not in stderr:
                raise
            return False
        self._runner.run(
            [
                self._gcloud, "iam", "service-accounts", "delete", email,
                "--project", self._project, "--quiet",
            ]
        )
        return True

    def gsa_email_for(self, name: str) -> str:
        # Same composition `ensure_gsa` uses (gsa_email + self._project) -
        # the destroy-access step (kobo_api.destroy) calls this to derive
        # the SAME email `ensure_gsa` would have returned, without creating
        # a GSA, so both sides can never drift apart.
        return gsa_email(name, self._project)

    def grant_bucket_prefix_write(self, gsa_email: str, bucket: str, prefix: str) -> None:
        # A CEL condition on the object name, not a plain (unconditional)
        # IAM binding: `roles/storage.objectAdmin` granted bucket-wide would
        # let this agent's identity read/write every OTHER agent's
        # replicated database too. `resource.name` for a GCS object is
        # `projects/_/buckets/<bucket>/objects/<object>` (the literal
        # "projects/_/" segment is GCS's own IAM condition convention, not
        # this project's id), so `startsWith(...prefix)` scopes the grant to
        # exactly this agent's own namespace and nothing wider.
        condition_title = f"agent-memory-replica-{prefix.rstrip('/')}"
        condition_expression = (
            f'resource.name.startsWith("projects/_/buckets/{bucket}/objects/{prefix}")'
        )
        self._runner.run(
            [
                self._gcloud, "storage", "buckets", "add-iam-policy-binding", f"gs://{bucket}",
                "--member", f"serviceAccount:{gsa_email}",
                "--role", "roles/storage.objectAdmin",
                "--condition", f"expression={condition_expression},title={condition_title}",
            ]
        )
        # Important 9 fix (paired with Critical 4, the composed replication
        # wrapper): `storage.objects.list` is a BUCKET-level permission --
        # it has no per-object `resource.name` to test, so the conditioned
        # grant above (which only covers get/create/delete-shaped object
        # operations against names matching the condition) denies it
        # outright, and litestream's replicate loop needs to list its
        # replica prefix's existing generations on startup to resume
        # correctly. A second, UNCONDITIONED binding grants exactly that:
        # `roles/storage.legacyBucketReader` (list-only, no object read/
        # write of its own) at the bucket level -- room-wide, since list
        # cannot be scoped narrower than the bucket, but still nothing
        # wider than list: no agent's identity gains read or write access
        # to another agent's replicated data through this second grant,
        # only the ability to enumerate object names. The restore drill
        # (an actual litestream restore against a live replica) remains
        # the one live proof this unblocks the replicate loop; this binding
        # is the fix that drill would otherwise catch missing.
        # The conditioned objectAdmin binding above means this policy now
        # holds a conditional binding, so gcloud's own conditional-binding
        # semantics require every subsequent add-iam-policy-binding call to
        # state its condition explicitly -- omitting the flag makes gcloud
        # PROMPT ("Binding condition:") instead of defaulting, and this
        # runs non-interactively (subprocess, no TTY), so an unanswered
        # prompt is a hang-turned-ERROR, not a default. `--condition=None`
        # is gcloud's own token for "explicitly no condition".
        self._runner.run(
            [
                self._gcloud, "storage", "buckets", "add-iam-policy-binding", f"gs://{bucket}",
                "--member", f"serviceAccount:{gsa_email}",
                "--role", "roles/storage.legacyBucketReader",
                "--condition", "None",
            ]
        )


# --- Agent (a2a chat over a kubectl port-forward) -----------------------------


class ChatMessage(TypedDict):
    """One turn of an a2a chat history."""

    role: str  # "user" | "agent"
    text: str


@runtime_checkable
class AgentClient(Protocol):
    def hello(self, namespace: str, service: str, text: str) -> str:
        """Say `text` to the agent's a2a endpoint; return its reply text."""
        ...

    def converse(self, namespace: str, service: str, messages: list[ChatMessage]) -> str:
        """Send the full `messages` history to the agent's a2a endpoint as
        one task; return its reply text."""
        ...

    def fetch_card(self, namespace: str, service: str) -> dict:
        """GET the agent's `/.well-known/agent-card.json` through the same
        port-forward flow as hello/converse; return the parsed JSON dict.

        Raises `ClientError` on ANY failure — an unreachable/still-booting
        pod, a non-200, or a non-JSON body all read the same way to
        `kobo_api.tooling.list_tooling`'s enforcement probe: "couldn't
        confirm enforcement", never a guess dressed up as one of the other
        failure modes.
        """
        ...


_FORWARDING_RE = re.compile(r"Forwarding from 127\.0\.0\.1:(\d+)")


def _wait_for_forwarded_port(reader, timeout_s: float = 10.0) -> int:
    """Read lines from `reader` (anything exposing `.readline() -> str`, like a
    subprocess's stdout) until kubectl's "Forwarding from 127.0.0.1:PORT" line
    appears; return the parsed port.

    `reader.readline()` blocks indefinitely on a real OS pipe (kubectl may
    simply never print the line), so it can't be called directly from the
    thread that owns the deadline: a deadline check only between calls does
    nothing to interrupt a call already in progress. Instead, a daemon thread
    drives `readline()` in a loop and pushes each line onto a `queue.Queue`;
    this function only ever blocks on `queue.Queue.get(timeout=...)`, computed
    against a monotonic deadline, so a hung `readline()` can never prevent
    this function (or a caller's `finally`-cleanup) from returning once the
    deadline passes. The reader thread is left running (it's a daemon, so it
    won't block interpreter exit); on a real subprocess, terminating/killing
    the process in the caller's `finally` closes the pipe and lets it exit.

    Factored out from `hello()` so this parsing logic is unit-testable against
    a fake reader, with no real subprocess involved.
    """
    lines: queue.Queue[str] = queue.Queue()

    def _pump() -> None:
        while True:
            line = reader.readline()
            lines.put(line)
            if not line:
                return

    threading.Thread(target=_pump, daemon=True).start()

    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ClientError("timed out waiting for port-forward")
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            raise ClientError("timed out waiting for port-forward") from None
        if not line:
            continue
        match = _FORWARDING_RE.search(line)
        if match:
            return int(match.group(1))


def _to_a2a_message(message: ChatMessage) -> dict:
    return {
        "kind": "message",
        "role": message["role"],
        "parts": [{"kind": "text", "text": message["text"]}],
    }


def _build_task_envelope(
    messages: list[ChatMessage], *, context_id: str | None = None
) -> dict:
    """Build the a2a task envelope for `messages`, in the switchboard's own
    dialect (live-verified against the deployed switchboard): the Task
    model requires a `status` field, and its convention is that
    `status.message` carries the CURRENT (last) turn while `history` holds
    only the turns that came before it, never the current one.

    A single message (the common case, e.g. `HelloStep`'s first-contact
    turn) has no prior turns, so `history` comes back `[]`; the message
    itself lands entirely in `status.message`.

    `context_id`, when given, is stamped onto the envelope as `contextId`
    (colleague-loops arc, Task 3): the switchboard's loop guard groups hops
    by contextId and is default-on, but without a caller-supplied id every
    hop looks like a fresh conversation and the guard is inert. Absent when
    not supplied, so the direct path and `HelloStep`'s first-contact turn
    keep their exact envelope.
    """
    *prior, current = messages
    envelope = {
        "kind": "task",
        "id": f"chat-{uuid.uuid4().hex}",
        "status": {
            "state": "submitted",
            "message": _to_a2a_message(current),
        },
        "history": [_to_a2a_message(message) for message in prior],
    }
    # The switchboard's loop guard groups hops by contextId and is default-on;
    # without a caller-supplied id every hop looks like a fresh conversation
    # and the guard is inert. Absent when not supplied, so the direct path
    # and first-contact keep their exact envelope.
    if context_id is not None:
        envelope["contextId"] = context_id
    return envelope


def _build_system_task_envelope(
    marker: str, text: str, *, metadata: dict | None = None
) -> dict:
    """A single-message task envelope carrying a system-task marker the
    daedalus runtime intercepts pre-model. `marker` names the lane
    (`teach`, `knowledge`, `tool`); `metadata` carries whatever that lane
    needs alongside it. The fast memory lane proved this shape for teach;
    every other instant write rides the same rail rather than opening a
    second door into the agent."""
    envelope = _build_task_envelope([{"role": "user", "text": text}])
    envelope["metadata"] = {"system_task": marker, **(metadata or {})}
    return envelope


def _build_teach_envelope(note: str, filename: str) -> dict:
    """The teach system task (fast-memory-lane spec). `note` is byte-for-byte
    the content the background archive commits to bundle/intake/<filename>,
    so both lanes agree on the content sha."""
    return _build_system_task_envelope("teach", note, metadata={"filename": filename})


def _build_learnings_envelope(on_date: str, known: list[dict] | None = None) -> dict:
    """The learnings system task: asks the agent for what people told it
    that day. The date and the `known` list (what is already pending,
    remembered, or skipped -- never to be re-proposed) ride metadata; the
    text is only a human-readable label for transcripts. Runtimes that
    predate `known` ignore it."""
    return _build_system_task_envelope(
        "learnings",
        f"Summarize your conversations from {on_date} into candidate memory snippets.",
        metadata={"date": on_date, "known": list(known or [])},
    )


def _join_text_parts(parts: object) -> str:
    """Join ALL text parts of an a2a message, in order — the family's
    join-all-parts contract (daedalus's `transports.a2a.text_of`), adopted
    here after the daedalus #13 review flagged `parts[0]["text"]` as a
    tripwire standardized in three places: it silently drops text the day
    any peer emits a multi-part reply. Parts without a `kind` key are
    accepted as text (this room's own older fixtures and some a2a servers
    omit it); parts whose `kind` says they are something else are skipped.
    Returns "" when nothing text-shaped is present — callers decide whether
    that is an error (direct path) or a try-the-next-source (relay path).
    """
    if not isinstance(parts, list):
        return ""
    texts = [
        p["text"]
        for p in parts
        if isinstance(p, dict)
        and isinstance(p.get("text"), str)
        and p.get("kind") in (None, "text")
    ]
    return "\n".join(t for t in texts if t)


def _post_task(client: httpx.Client, port: int, messages: list[ChatMessage]) -> str:
    """POST the a2a task envelope for `messages` to the forwarded port; return
    the reply text.

    Factored out from `_send_task()` so the request/response shape is
    unit-testable against an httpx.MockTransport-backed client, with no real
    network involved.

    httpx transport errors (timeouts, connection failures, ...) aren't
    ClientError, so left alone they'd surface as raw httpx exceptions to
    callers that only expect ClientError. Wrap them here, preserving the
    original exception as the cause.

    A 200 response doesn't guarantee a well-formed reply: a non-JSON body
    raises json.JSONDecodeError (a ValueError), and JSON missing the expected
    path raises KeyError/IndexError/TypeError — none of those are ClientError
    either, so they're wrapped the same way.
    """
    try:
        resp = client.post(f"http://127.0.0.1:{port}/a2a/tasks", json=_build_task_envelope(messages))
    except httpx.HTTPError as exc:
        raise ClientError(f"a2a request failed: {exc!r}") from exc
    if resp.status_code != 200:
        raise ClientError(f"a2a request failed ({resp.status_code}): {resp.text}")
    try:
        text = _join_text_parts(resp.json()["status"]["message"]["parts"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ClientError(
            f"malformed a2a reply: {exc!r} (body: {resp.text[:200]!r})"
        ) from exc
    if not text:
        raise ClientError(f"malformed a2a reply: no text parts (body: {resp.text[:200]!r})")
    return text


def _get_card(client: httpx.Client, port: int) -> dict:
    """GET the forwarded port's `/.well-known/agent-card.json`; return the
    parsed JSON dict.

    Factored out from `KubectlAgentClient.fetch_card` the same way
    `_post_task` is factored out of `_send_task` — unit-testable against an
    httpx.MockTransport-backed client, with no real network involved.

    Same wrapping discipline as `_post_task`: a transport error (timeout,
    connection failure, ...) isn't `ClientError`, so it's wrapped here rather
    than left to escape to a caller that only expects `ClientError`. A 200
    response doesn't guarantee a well-formed card — a non-JSON body raises
    `json.JSONDecodeError` (a `ValueError`), also wrapped. Unlike
    `_post_task`, there's no reply-path shape to validate beyond "valid
    JSON": the card's own structure (an `enforcement` key, its `version`,
    ...) is `kobo_api.tooling.list_tooling`'s concern, not this transport
    helper's.
    """
    try:
        resp = client.get(f"http://127.0.0.1:{port}/.well-known/agent-card.json")
    except httpx.HTTPError as exc:
        raise ClientError(f"fetch_card request failed: {exc!r}") from exc
    if resp.status_code != 200:
        raise ClientError(f"fetch_card request failed ({resp.status_code}): {resp.text}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ClientError(
            f"malformed agent card: {exc!r} (body: {resp.text[:200]!r})"
        ) from exc


class KubectlAgentClient:
    """Reaches an in-cluster agent's a2a endpoint (or its well-known card) via
    a kubectl port-forward.

    Spawns `kubectl -n <namespace> port-forward svc/<service> :80`, parses the
    ephemeral local port it reports, runs one request against it (a POSTed
    a2a task for hello/converse, a GET of `/.well-known/agent-card.json` for
    `fetch_card`), and always terminates the port-forward afterward (success
    or failure) — see `_over_port_forward` for the shared machinery.
    """

    def __init__(
        self,
        kubectl: str = "kubectl",
        port_forward_timeout_s: float = 10.0,
        http_client: httpx.Client | None = None,
    ):
        self._kubectl = kubectl
        self._timeout_s = port_forward_timeout_s
        # The hello POST waits on a full model roundtrip (routinely >5s
        # live), well past httpx's bare 5s default — give it generous
        # headroom rather than let a slow-but-healthy reply read as a
        # timeout/failure.
        self._http_client = http_client or httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))

    def hello(self, namespace: str, service: str, text: str) -> str:
        return self.converse(namespace, service, [{"role": "user", "text": text}])

    def converse(self, namespace: str, service: str, messages: list[ChatMessage]) -> str:
        return self._send_task(namespace, service, messages)

    def fetch_card(self, namespace: str, service: str) -> dict:
        return self._over_port_forward(namespace, service, lambda port: _get_card(self._http_client, port))

    def _send_task(self, namespace: str, service: str, messages: list[ChatMessage]) -> str:
        return self._over_port_forward(
            namespace, service, lambda port: _post_task(self._http_client, port, messages)
        )

    def _over_port_forward(self, namespace: str, service: str, fn: Callable[[int], dict | str]):
        """Spawn `kubectl -n <namespace> port-forward svc/<service> :80`,
        wait for the ephemeral local port kubectl reports, call `fn(port)`,
        and always terminate the port-forward afterward (success or
        failure) — the exact Popen/wait/cleanup dance `_send_task` always
        had, factored out once `fetch_card` needed the identical machinery
        for a GET instead of a POST.
        """
        # Popen itself can fail before any subprocess exists (e.g. `kubectl`
        # missing from PATH) — that's OSError, not ClientError, so it's
        # wrapped here rather than left to escape to callers that only
        # expect ClientError.
        try:
            proc = subprocess.Popen(
                [self._kubectl, "-n", namespace, "port-forward", f"svc/{service}", ":80"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            raise ClientError(f"failed to start port-forward: {exc}") from exc
        try:
            port = _wait_for_forwarded_port(proc.stdout, self._timeout_s)
            return fn(port)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # Best-effort zombie reap: kill() already sent SIGKILL,
                    # so the process is dying (or dead) regardless — this
                    # second wait is just cleanup, never worth raising or
                    # blocking longer over.
                    pass


# --- Kubectl (rollout status) -----------------------------------------------------


@runtime_checkable
class KubectlClient(Protocol):
    def rollout_ok(self, namespace: str, deploy: str, timeout_s: int = 180) -> bool: ...

    def deploy_env_names(self, namespace: str, deploy: str) -> list[str]:
        """Container 0's env var names on the *live* Deployment, in spec order
        (empty list when it has none).

        Defense-in-depth for the stale-apply race (see DeployStep,
        `kobo_api/steps.py`): `rollout_ok` only proves *some* generation of
        the Deployment rolled out cleanly, not that it's the generation
        carrying WireStep's contract — a racing `kubectl apply` from a
        concurrent CI run can land an older, unwired tree last and still
        report a clean rollout. This reads the actual live spec so DeployStep
        can check it directly instead of trusting rollout_ok alone.
        """
        ...

    def deploy_service_account(self, namespace: str, deploy: str) -> str:
        """The live Deployment's pod template `serviceAccountName` (empty
        string when unset). Same stale-apply defense as `deploy_env_names`."""
        ...

    def deploy_image(self, namespace: str, deploy: str) -> str:
        """Container 0's image reference on the *live* Deployment (e.g.
        `.../gojira-app:abc123def456`).

        Used by the knowledge module's delivery check (`kobo_api.knowledge`)
        to tell whether a saved edit has actually rolled out yet: generated
        CI tags the image it builds with the first 12 chars of the commit
        sha it built from, so comparing this tag against a known commit's
        `sha[:12]` is a stateless way to answer "is that commit live" without
        any of kobo's own state to keep in sync.
        """
        ...

    def apply_secret(
        self,
        namespace: str,
        name: str,
        literals: dict[str, str],
        *,
        dry_run: bool = False,
    ) -> None:
        """Idempotent upsert of a generic Secret `name` in `namespace` whose
        data is exactly `literals` (e.g. `{"SLACK_BOT_TOKEN": ..., "SLACK_APP_TOKEN": ...}`).

        `dry_run=True` asks the API SERVER whether this write would be
        allowed -- authorization and admission run in full, nothing is
        persisted. It exists for `KubeSlackTokenStore.check_writable`: a
        boot-time probe that actually wrote would be a lost update, since
        another replica can rotate the pair between this process's read and
        its write, and writing back what it read would restore a refresh
        token Slack has already spent.

        Used by `kobo_api.slack_setup.wire_slack` to deliver the pasted
        Slack tokens to the agent's namespace: a `create secret --dry-run
        -o yaml | apply -f -` round trip rather than a plain `create` (which
        would fail on a second call for the same name) — safe to call again
        with a rotated token pair. The literal values reach kubectl only via
        this call's argv/stdin; they are never logged, never written to a
        file kobo controls, and never appear in a raised exception's
        message (the real implementation calls `CommandRunner.run` with
        `redact_args=True` on both stages, so a debug-level log never embeds
        the argv either, and separately sanitizes CommandRunner's own
        argv-embedding failure message on both stages for exactly this
        reason).
        """
        ...

    def secret_literals(self, namespace: str, name: str) -> dict[str, str]:
        """The Secret `name`'s data as plain, base64-DECODED values — the
        read half of `apply_secret` above.

        Used by `KubeSlackTokenStore` (below) to keep the Slack
        config-token pair in a Secret rather than on a ReadWriteOnce disk,
        so the room can run more than one replica. An absent Secret raises
        `ClientError`; a Secret that exists with no data reads as `{}` (the
        store above decides which missing KEY is fatal and says which).

        The returned values are live credentials and this call's stdout
        carries them: no implementation may log that stdout, nor let it
        reach a raised exception — see `KubectlCLI.secret_literals` for
        what that costs (an unchained re-raise, since `CommandRunner`'s own
        failure message embeds `stderr or stdout`). Errors name only
        `namespace`/`name`, never a value.
        """
        ...

    def deployment_status_json(self, namespace: str, deploy: str) -> dict:
        """The live Deployment's full status as a decoded JSON dict
        (`kubectl get deploy ... -o json`).

        Used by `kobo_api.knowledge.rollout_in_progress` (the chat route's
        delivery-aware-copy probe, Task 6) to tell whether the Deployment is
        genuinely mid-rollout right now — distinct from `deploy_image`'s
        stale-apply defense above, which only reads the live pod template's
        image, not rollout progress.
        """
        ...

    def pods_json(self, namespace: str) -> list[dict]:
        """Every pod in the namespace (`kubectl get pods -o json`'s
        `items`), for the health probe's restart counts and wait reasons
        (kobo_api.health)."""
        ...

    def restart_deploy(self, namespace: str, deploy: str) -> None:
        """Ask kubectl to roll the Deployment's pods now (`kubectl rollout
        restart`).

        Used by the tooling fast lane (kobo_api.main's tooling upload route,
        arc3d Task 5): a fast-delivered tool lands on the agent's persistent
        volume immediately, but the runtime's enforcement loader only
        re-reads its tools directories at boot -- no hot reload into the
        running process. This is the exact nudge that makes the boot
        happen, requested once per successful delivery.
        """
        ...

    def external_secret_status(self, namespace: str, name: str) -> dict | None:
        """The ExternalSecret `name`'s own `.status` as a decoded dict, or
        None when there is no such object in `namespace`.

        The read behind the Secrets panel's lock (design 2026-09-23): a key
        name in a list is not a credential in a pod, and this object's Ready
        condition is the only honest evidence that it got there.

        This is deliberately NOT a read of the Kubernetes Secret the object
        owns. `secret_literals` above exists for one caller that has to hold
        the room's own Slack tokens; nothing about showing an owner whether
        their secret arrived needs a value, so nothing here asks for one. An
        ExternalSecret's own body names paths and keys and holds no value at
        all.

        Absent is an ANSWER, not a failure: an agent whose owner has never
        added a secret has no such object, which is the ordinary case. Every
        other failure raises `ClientError`.
        """
        ...


def image_tag(image: str) -> str | None:
    """The tag of a container image reference, or None when it has none.

    Handles all three shapes kubectl can hand back: `repo:tag`,
    `repo:tag@sha256:digest` (digest-pinned apply — the platform's
    generated CI moved to this), and `repo@sha256:digest` (no tag at
    all). The digest part is stripped FIRST, so the trailing-colon split
    can never mistake the digest hex for a tag. A repo with a port but
    no tag (`registry:5000/repo`) also returns None (the split segment
    contains a `/`).
    """
    base = image.split("@", 1)[0]
    tail = base.rsplit(":", 1)
    if len(tail) != 2 or "/" in tail[1]:
        return None
    return tail[1]


class KubectlCLI:
    """kubectl-backed KubectlClient."""

    def __init__(self, runner: CommandRunner | None = None, kubectl: str = "kubectl"):
        self._runner = runner or CommandRunner()
        self._kubectl = kubectl

    def rollout_ok(self, namespace: str, deploy: str, timeout_s: int = 180) -> bool:
        try:
            self._runner.run(
                [
                    self._kubectl, "-n", namespace, "rollout", "status",
                    f"deploy/{deploy}", f"--timeout={timeout_s}s",
                ]
            )
            return True
        except ClientError:
            return False

    def deploy_env_names(self, namespace: str, deploy: str) -> list[str]:
        out = self._runner.run(
            [
                self._kubectl, "-n", namespace, "get", "deploy", deploy,
                "-o", "jsonpath={.spec.template.spec.containers[0].env[*].name}",
            ]
        )
        # kubectl's jsonpath template separates repeated matches with a
        # single space; CommandRunner.run() has already .strip()'d the
        # output, so a plain split() both tokenizes on that separator and
        # turns "" (no env vars) into [] rather than [""].
        return out.split()

    def deploy_service_account(self, namespace: str, deploy: str) -> str:
        return self._runner.run(
            [
                self._kubectl, "-n", namespace, "get", "deploy", deploy,
                "-o", "jsonpath={.spec.template.spec.serviceAccountName}",
            ]
        )

    def deploy_image(self, namespace: str, deploy: str) -> str:
        return self._runner.run(
            [
                self._kubectl, "-n", namespace, "get", "deploy", deploy,
                "-o", "jsonpath={.spec.template.spec.containers[0].image}",
            ]
        )

    def deployment_status_json(self, namespace: str, deploy: str) -> dict:
        out = self._runner.run(
            [self._kubectl, "-n", namespace, "get", "deploy", deploy, "-o", "json"]
        )
        return json.loads(out)

    def pods_json(self, namespace: str) -> list[dict]:
        out = self._runner.run([self._kubectl, "-n", namespace, "get", "pods", "-o", "json"])
        return list((json.loads(out) or {}).get("items") or [])

    def restart_deploy(self, namespace: str, deploy: str) -> None:
        self._runner.run(
            [self._kubectl, "-n", namespace, "rollout", "restart", f"deploy/{deploy}"]
        )

    def external_secret_status(self, namespace: str, name: str) -> dict | None:
        # `--ignore-not-found` is what makes "there is no such object" an
        # empty success rather than an exit code this would have to tell
        # apart from a cluster it could not reach at all. Those two are
        # different answers to an owner -- nothing is delivering this vs.
        # the room could not look -- and reading one as the other is the
        # whole bug this flag avoids.
        out = self._runner.run(
            [
                self._kubectl, "-n", namespace, "get", "externalsecret", name,
                "--ignore-not-found", "-o", "json",
            ]
        )
        if not out.strip():
            return None
        try:
            data = json.loads(out)
        except ValueError:
            # Not chained and not quoted, the same discipline every other
            # read of somebody else's object here holds: the room says
            # which object it could not read and nothing about what was
            # in it.
            raise ClientError(
                f"kubectl get externalsecret {namespace}/{name} "
                "answered something this room could not read"
            ) from None
        status = data.get("status") if isinstance(data, dict) else None
        # An object that exists and has no status yet is one the platform
        # has not got to: an empty mapping reads as `syncing`, which is
        # exactly what it is.
        return status if isinstance(status, dict) else {}

    def secret_literals(self, namespace: str, name: str) -> dict[str, str]:
        try:
            out = self._runner.run(
                [self._kubectl, "-n", namespace, "get", "secret", name, "-o", "json"]
            )
        except ClientError as exc:
            # NOT `from exc`, unlike every other re-raise in this class:
            # CommandRunner.run's message is built from `stderr or stdout`,
            # and THIS command's stdout is the Secret's own (base64) data.
            # A chained cause is printed in full by any traceback, so the
            # sanitized message here would be worth nothing with the
            # original still hanging off it. The underlying stdout/stderr
            # are dropped for the same reason; only the exit code survives.
            raise ClientError(
                f"kubectl get secret {namespace}/{name} failed",
                returncode=exc.returncode,
            ) from None

        try:
            data = json.loads(out).get("data") or {}
            return {
                key: base64.b64decode(value).decode() for key, value in data.items()
            }
        except (ValueError, TypeError, AttributeError):
            # Every way this read can be malformed, and all of them
            # ValueError-family or a shape that isn't a mapping: bad JSON,
            # a non-object payload, a value that isn't base64 or isn't
            # UTF-8 underneath. Same no-chaining rule as above -- `out` IS
            # the secret material, and a JSONDecodeError/binascii error
            # embeds the text it choked on. Never name a key here either: a
            # malformed read gives no ground to trust which key the failure
            # belongs to.
            raise ClientError(
                f"kubectl get secret {namespace}/{name} returned data this room "
                "could not read"
            ) from None

    def apply_secret(
        self,
        namespace: str,
        name: str,
        literals: dict[str, str],
        *,
        dry_run: bool = False,
    ) -> None:
        literal_args = [f"--from-literal={key}={value}" for key, value in literals.items()]
        try:
            # redact_args=True: this stage's argv is the only place either
            # token literal ever appears (final-review Finding 3) — without
            # it, CommandRunner.run's own debug log would put the raw
            # `--from-literal=KEY=value` argv (secret values and all) into
            # logs one `--log-level debug` away. The stage-2 call below
            # passes it too, defense-in-depth, even though its own argv
            # never carries a literal.
            manifest_yaml = self._runner.run(
                [
                    self._kubectl, "-n", namespace, "create", "secret", "generic", name,
                    *literal_args, "--dry-run=client", "-o", "yaml",
                ],
                redact_args=True,
            )
        except ClientError as exc:
            # CommandRunner.run's own ClientError message embeds the full
            # argv it ran — which, for this specific command, is the only
            # place the literal secret values ever appear. Re-raise with a
            # message built from just the underlying stderr/stdout instead,
            # so a failure here can never leak `literals`' values the way
            # letting the original exception propagate would. Unchained for
            # the same reason: a chained cause prints its own message, argv
            # and all, in any traceback. stdout is dropped because this
            # stage's stdout is the rendered Secret manifest.
            raise ClientError(
                f"kubectl create secret {name!r} (dry-run) failed: {exc.stderr}",
                returncode=exc.returncode, stdout="", stderr=exc.stderr,
            ) from None

        try:
            # `--dry-run=server` (never `client`): the whole question a
            # caller asks with dry_run is whether the API SERVER would let
            # this write through, which means reaching its authorization
            # and admission chain. A client dry-run never leaves the
            # machine and would answer nothing.
            apply_argv = [self._kubectl, "apply", "-f", "-"]
            if dry_run:
                apply_argv.append("--dry-run=server")
            self._runner.run(apply_argv, stdin=manifest_yaml, redact_args=True)
        except ClientError as exc:
            # Same reasoning as above, defense-in-depth: this stage's argv
            # carries no secret material (the manifest is piped via stdin),
            # but sanitizing both stages identically means the safety
            # property doesn't depend on which stage happens to fail.
            raise ClientError(
                f"kubectl apply of secret {name!r} failed: {exc.stderr or exc.stdout}",
                returncode=exc.returncode, stdout=exc.stdout, stderr=exc.stderr,
            ) from None


# --- Slack (Web API: manifest app creation + config-token rotation) -----------


_SLACK_API_BASE = "https://slack.com/api"
_SLACK_TOKEN_KEY = "slack_app_config_token"
_SLACK_REFRESH_KEY = "slack_app_config_refresh_token"
# Slack's only two config-token failure modes that mean "rotate and retry" —
# anything else is a real problem the caller needs to see, not something a
# blind rotate-on-any-failure should paper over.
_SLACK_AUTH_ERRORS = frozenset({"token_expired", "invalid_auth"})

# apps.manifest.delete's two "the app is already gone" error codes — both
# grounded in Slack's own documented error table for the method
# (docs.slack.dev/reference/methods/apps.manifest.delete, fetched 2026-08-17):
# "invalid_app_id" ("The app ID passed in was invalid.") and "app_not_found"
# ("The specified app was not found."). Every other documented error there
# (bad auth aside, handled by _SLACK_AUTH_ERRORS' rotate-and-retry) is a real
# failure, never an already-gone signal.
_SLACK_ALREADY_GONE_ERRORS = frozenset({"app_not_found", "invalid_app_id"})


@runtime_checkable
class SlackClient(Protocol):
    def create_app(self, manifest: dict) -> str:
        """Create a Slack app from `manifest`; return its app_id."""
        ...

    def delete_app(self, app_id: str) -> bool:
        """Delete the Slack app `app_id` via apps.manifest.delete (Task 3:
        agent destruction's Slack step), same config-token + rotate-on-401
        discipline as `create_app`.

        Returns True when Slack actually deleted the app. Returns False
        (never raises) when Slack reports it's already gone
        (`_SLACK_ALREADY_GONE_ERRORS`) — the idempotency signal the
        destroy step treats as done. Any other failure raises
        `SlackApiError`.
        """
        ...


def _read_slack_tokens(tokens_path: Path) -> tuple[str, str]:
    """Parse `tokens_path`'s two `key: value` lines; returns (config_token,
    refresh_token).

    Deliberately a tolerant line-based parse rather than a full YAML load:
    the file mixes free-form comment lines with exactly two `key: value`
    lines (see module docstring context — this mirrors the rotation
    tooling's own approach), and the same line-oriented reasoning lets
    `_write_slack_tokens` below replace just those two lines on rewrite
    without disturbing anything else in the file.

    Both a missing file and one missing either required key raise
    `ClientError` naming only the path — never the file's contents, which
    may itself hold partial token material.
    """
    try:
        lines = tokens_path.read_text().splitlines()
    except OSError as exc:
        raise ClientError(f"slack tokens file not found: {tokens_path}") from exc

    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        values[key.strip()] = value.strip()

    try:
        return values[_SLACK_TOKEN_KEY], values[_SLACK_REFRESH_KEY]
    except KeyError as exc:
        raise ClientError(f"slack tokens file missing required keys: {tokens_path}") from exc


def _write_slack_tokens(tokens_path: Path, config_token: str, refresh_token: str) -> None:
    """Rewrite `tokens_path` with a fresh (config_token, refresh_token) pair,
    preserving every comment/blank line untouched and replacing only the two
    `key: value` lines.

    Written atomically (tmp file + `os.replace`) so a crash mid-write can
    never leave a half-written file: Slack's refresh token is single-use, so
    losing the just-returned new pair after already spending the old one
    would lock kobo out of rotating ever again.
    """
    try:
        lines = tokens_path.read_text().splitlines(keepends=True)
    except OSError as exc:
        raise ClientError(f"slack tokens file not found: {tokens_path}") from exc

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{_SLACK_TOKEN_KEY}:"):
            new_lines.append(f"{_SLACK_TOKEN_KEY}: {config_token}\n")
        elif stripped.startswith(f"{_SLACK_REFRESH_KEY}:"):
            new_lines.append(f"{_SLACK_REFRESH_KEY}: {refresh_token}\n")
        else:
            new_lines.append(line)

    tmp_path = tokens_path.with_name(tokens_path.name + ".tmp")
    # Create the tmp file at 0600 BEFORE writing its content, rather than a
    # plain `write_text()` (which creates it at umask-default perms, e.g.
    # 0644): this file holds a live Slack refresh token from the moment its
    # first byte lands, and `copymode` below only fixes the mode up AFTER
    # the full content is already written — closing that transient window
    # rather than narrowing it.
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(new_lines))
    if tokens_path.exists():
        # Preserve the original file's mode on the tmp file: the tmp file
        # gets umask-default permissions (e.g. 0644), and os.replace carries
        # the SOURCE's mode — so without this, a tokens file an operator
        # hardened to 0600 would silently widen to 0644 after rotation. This
        # file holds a live Slack refresh token.
        shutil.copymode(tokens_path, tmp_path)
    os.replace(tmp_path, tokens_path)


# The two keys a `KubeSlackTokenStore`'s Secret holds. Deliberately shorter
# and flatter than the file store's `slack_app_config_token`/`slack_app_
# config_refresh_token` line keys: a Secret's data keys must be valid
# config-map key names, and an operator creating this Secret by hand types
# them (`--from-literal=config_token=...`).
_SECRET_TOKEN_KEY = "config_token"
_SECRET_REFRESH_KEY = "refresh_token"

# How hard a freshly rotated pair is persisted before the room gives up on
# it. The Kube store's write is two kubectl round trips over the network,
# attempted at the one moment a failure is unrecoverable: Slack has already
# spent the old refresh token, so a pair lost here is lost for good. A few
# attempts with a short backoff cover the transient failures (a timeout, a
# brief API-server blip) that make up nearly all of them.
SLACK_STORE_WRITE_ATTEMPTS = 3
SLACK_STORE_WRITE_BACKOFF_S = 0.5

# What an operator is told when that final attempt fails. It names the exact
# state the room is in and the only move that recovers it -- never a value,
# and never a token fragment.
SLACK_ROTATION_LOST = (
    "Slack rotated the app-configuration token pair, but this room could not "
    "store the new pair, and the pair it replaced is now spent. Re-issue the "
    "token pair on Slack's app configuration page and update the room's token "
    "store with it."
)


@runtime_checkable
class SlackTokenStore(Protocol):
    """Where the Slack app-configuration pair lives between rotations.

    The seam exists because the pair is the only mutable state kobo keeps
    outside Postgres, and a file on a ReadWriteOnce disk is what pins the
    room to a single replica. The room's Postgres never holds a credential,
    so the two-replica answer is a Kubernetes Secret, not a table.

    Implementations must be safe to read at any moment from any replica:
    `HttpxSlackClient` re-reads before spending a refresh token and again
    after a rotate fails, and treats a changed pair as "another process
    rotated", so a store that cached would defeat the whole arrangement.
    """

    def read(self) -> tuple[str, str]:
        """The current (config_token, refresh_token). Raises `ClientError`
        when the pair cannot be read — never naming a value."""
        ...

    def write(self, config_token: str, refresh_token: str) -> None:
        """Persist a freshly rotated pair, both values together."""
        ...

    def check_writable(self) -> None:
        """Raise `ClientError` (naming only the store) if this process
        could not persist a rotated pair right now. Must PERSIST NOTHING:
        the point is to learn at boot what would otherwise only surface
        ~12h later, at the one moment a failure costs the live pair."""
        ...


class FileSlackTokenStore:
    """The gitignored local YAML file (`slack_tokens_path`): local dev,
    single-process installs, and every test that predates this seam.

    A thin wrapper over `_read_slack_tokens`/`_write_slack_tokens`, whose
    behavior — tolerant line parse, comment-preserving atomic rewrite,
    errors naming only the path — is unchanged and still the contract.
    """

    def __init__(self, path: Path):
        self._path = path

    def read(self) -> tuple[str, str]:
        return _read_slack_tokens(self._path)

    def write(self, config_token: str, refresh_token: str) -> None:
        _write_slack_tokens(self._path, config_token, refresh_token)

    def check_writable(self) -> None:
        """Nothing to check. The write is a local atomic rewrite of a file
        this process already reads, with no authorization step that could
        succeed for the read and fail for the write -- and a dry run that
        actually touched the file would be a worse answer than no answer."""


class KubeSlackTokenStore:
    """A Kubernetes Secret holding the pair under `config_token` /
    `refresh_token` — the store that lets the room run more than one
    replica, since every replica reads and writes the same object instead
    of its own copy of a mounted file.

    Reads go through `KubectlClient.secret_literals` and writes through the
    same `apply_secret` upsert the room already delivers agent Slack tokens
    with, so nothing new touches a credential on its way to kubectl. Both
    keys are always written together: a half-written pair would leave the
    room holding a config token it cannot rotate.
    """

    def __init__(self, kubectl: KubectlClient, namespace: str, name: str):
        self._kubectl = kubectl
        self._namespace = namespace
        self._name = name

    def read(self) -> tuple[str, str]:
        literals = self._kubectl.secret_literals(self._namespace, self._name)
        for key in (_SECRET_TOKEN_KEY, _SECRET_REFRESH_KEY):
            if not literals.get(key):
                raise ClientError(
                    f"slack tokens secret {self._namespace}/{self._name} is "
                    f"missing key {key!r}"
                )
        return literals[_SECRET_TOKEN_KEY], literals[_SECRET_REFRESH_KEY]

    def write(self, config_token: str, refresh_token: str) -> None:
        self._kubectl.apply_secret(
            self._namespace,
            self._name,
            {_SECRET_TOKEN_KEY: config_token, _SECRET_REFRESH_KEY: refresh_token},
        )

    def check_writable(self) -> None:
        """Ask the API server whether this room could persist a rotated
        pair, and persist nothing (`apply_secret(dry_run=True)`).

        Deliberately NOT a write-back of the pair just read: `kubectl
        apply` is last-write-wins, so another replica rotating inside that
        window would have its brand-new pair overwritten with the one this
        process read moments earlier -- whose refresh token Slack has
        already spent. A probe that can cause the lockout it screens for is
        worse than no probe.

        The pair it just read is what gets submitted, so admission sees a
        realistically-shaped object rather than a placeholder.
        """
        config_token, refresh_token = self.read()
        try:
            self._kubectl.apply_secret(
                self._namespace,
                self._name,
                {_SECRET_TOKEN_KEY: config_token, _SECRET_REFRESH_KEY: refresh_token},
                dry_run=True,
            )
        except ClientError as exc:
            # `apply_secret`'s message names the Secret but not its
            # namespace; the caller logs this verbatim, so say which one.
            raise ClientError(
                f"slack tokens secret {self._namespace}/{self._name} is not "
                f"writable by this room: {exc}"
            ) from None


def slack_token_store(config: Config, kubectl: KubectlClient) -> SlackTokenStore | None:
    """The store `Clients.real` hands `HttpxSlackClient`, or None when
    Slack connect isn't configured at all.

    This is the ONE place "is Slack wired" is decided: returning None here
    is what leaves `Clients.slack` None, and every other call site in the
    room (the connect route's 503, the destroy step) branches on that field
    rather than re-deriving the question from config — so neither store can
    be added or removed without every gate following automatically.

    The Secret wins when both are set: a room that names a Secret is a
    deployed room, and silently rotating into a mounted file there would
    put the live pair somewhere only one replica can see. One WARNING says
    so (the app configures no logging, so INFO would never reach pod logs
    — same reasoning as `kobo_api.org._disclose_overrides`).
    """
    if config.slack_tokens_secret:
        if config.slack_tokens_path:
            log.warning(
                "slack_tokens_secret is set, so slack_tokens_path (%s) is ignored: "
                "the config-token pair lives in the Secret",
                config.slack_tokens_path,
            )
        namespace, _, name = config.slack_tokens_secret.partition("/")
        return KubeSlackTokenStore(kubectl, namespace, name)
    if config.slack_tokens_path:
        return FileSlackTokenStore(config.slack_tokens_path)
    return None


def probe_slack_token_store(clients: Clients) -> None:
    """Ask at boot whether this room could persist a rotated Slack token
    pair, and change nothing either way (`SlackTokenStore.check_writable`).

    Rotation's failure mode is silent and delayed: a room with read-only
    access to that Secret works perfectly until the config token expires
    ~12h later, and discovers it cannot store the replacement at the exact
    moment the old pair has already been spent (see `_persist`). This turns
    that into one ERROR at startup, hours before it can cost anything.

    Never raises and never blocks boot: a room that cannot reach the Secret
    right now still serves every route that has nothing to do with Slack.

    Called once per process from `kobo_api.main.build_default_app`, never
    from `create_app`, so tests that build their own app see nothing of it.
    """
    store = getattr(clients.slack, "token_store", None)
    if store is None:
        return
    try:
        store.check_writable()
    except ClientError as exc:
        log.error(
            "%s: slack app-config token rotation will fail until the room can "
            "write it",
            exc,
        )


class HttpxSlackClient:
    """Slack Web API client for app-manifest creation, with automatic
    config-token rotation.

    Slack's app-config tokens expire in ~12h; `create_app` reads the current
    token from the store on every call (never cached across calls) so a
    rotation from a previous call — or from outside this process entirely —
    is always picked up fresh. On `ok: false` with error `token_expired` or
    `invalid_auth`, it rotates the pair exactly once (persisting the new pair
    to the store before using it) and retries the create exactly once;
    a second auth failure after that raises rather than looping forever.

    With two replicas both replicas can hit the same expired token at the
    same moment, and there is no lock: `_rotate` instead makes the race
    convergent by re-reading the store around Slack's single-use refresh
    token. See its docstring.
    """

    def __init__(
        self,
        store: SlackTokenStore | Path,
        http_client: httpx.Client | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        # A bare Path still works, and means the file store: every caller
        # and test that predates the seam passes one.
        self._store = FileSlackTokenStore(store) if isinstance(store, Path) else store
        self._client = http_client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        # Injected purely so the persist retry's backoff costs a test
        # nothing; no caller in the room passes anything but the default.
        self._sleep = sleep

    @property
    def token_store(self) -> SlackTokenStore:
        """The store this client rotates against. Public because the
        boot-time probe (`probe_slack_token_store`) needs it, and reaching
        through a private attribute from another module for that would make
        the seam an accident rather than a contract."""
        return self._store

    def create_app(self, manifest: dict) -> str:
        config_token, _ = self._store.read()
        return self._create_app(manifest, config_token, retried=False)

    def _create_app(self, manifest: dict, config_token: str, *, retried: bool) -> str:
        resp = self._client.post(
            f"{_SLACK_API_BASE}/apps.manifest.create",
            data={"manifest": json.dumps(manifest)},
            headers={"Authorization": f"Bearer {config_token}"},
        )
        body = resp.json()
        if body.get("ok"):
            return body["app_id"]

        error = body.get("error", "unknown_error")
        if error in _SLACK_AUTH_ERRORS and not retried:
            new_config_token = self._rotate(config_token)
            return self._create_app(manifest, new_config_token, retried=True)

        # Never include `manifest` or any token material here — just Slack's
        # own error code.
        raise SlackApiError(f"slack app creation failed: {error}")

    def _rotate(self, failed_config_token: str) -> str:
        """Rotate the config-token pair via `tooling.tokens.rotate` and
        persist the new pair to the store BEFORE returning it — see the
        class docstring and `_write_slack_tokens` for why persist-first is
        load-bearing (the refresh token is single-use).

        The same single-use refresh token is what makes two replicas
        dangerous: both can reach this at once, and the loser of that race
        would spend a token the winner already spent. Rather than a lock,
        the store is re-read at both points where the winner's work is
        already visible in it:

        - before calling Slack, if the stored config token is no longer the
          one that just failed auth, someone else has already rotated —
          their token is returned and Slack is never called;
        - after a failed rotate, if the stored refresh token is no longer
          the one this call spent, someone else's rotate landed in between
          and their config token is returned instead of raising.

        A rotate that fails with the store untouched is a real failure and
        still raises, exactly as before.
        """
        stored_config_token, refresh_token = self._store.read()
        if stored_config_token != failed_config_token:
            log.warning(
                "slack config token was rotated by another process; using the "
                "pair now in the store instead of rotating again"
            )
            return stored_config_token

        resp = self._client.post(
            f"{_SLACK_API_BASE}/tooling.tokens.rotate",
            data={"refresh_token": refresh_token},
        )
        body = resp.json()
        if not body.get("ok"):
            error = body.get("error", "unknown_error")
            try:
                current_config_token, current_refresh_token = self._store.read()
            except ClientError:
                # A store that can't be re-read tells us nothing about who
                # won; report Slack's own failure rather than this one.
                current_config_token, current_refresh_token = "", refresh_token
            if current_refresh_token != refresh_token:
                log.warning(
                    "slack token rotation failed (%s) because another process "
                    "spent the refresh token first; using the pair it stored",
                    error,
                )
                return current_config_token
            raise SlackApiError(f"slack token rotation failed: {error}")

        new_config_token = body["token"]
        new_refresh_token = body["refresh_token"]
        self._persist(new_config_token, new_refresh_token)
        return new_config_token

    def _persist(self, config_token: str, refresh_token: str) -> None:
        """Write a freshly issued pair to the store, retried.

        The one genuinely unrecoverable moment in this class: Slack has
        already spent the old refresh token by the time this runs, so a
        pair that never lands in the store is gone, and the room can never
        rotate again without a human re-issuing it. The Kube store's write
        is two kubectl round trips, so the failures worth surviving are
        exactly the transient ones -- hence the retry, and hence a final
        failure that says what has to be done rather than only that
        something broke.
        """
        for attempt in range(1, SLACK_STORE_WRITE_ATTEMPTS + 1):
            try:
                self._store.write(config_token, refresh_token)
                return
            except ClientError as exc:
                # The store's own messages name only the file path or the
                # namespace/name, never a value -- see FileSlackTokenStore
                # and KubeSlackTokenStore.
                log.warning(
                    "slack token store write failed (attempt %d of %d): %s",
                    attempt,
                    SLACK_STORE_WRITE_ATTEMPTS,
                    exc,
                )
                if attempt < SLACK_STORE_WRITE_ATTEMPTS:
                    self._sleep(SLACK_STORE_WRITE_BACKOFF_S * attempt)

        log.error("%s", SLACK_ROTATION_LOST)
        # Unchained: the underlying store error is already logged above, and
        # what the caller needs here is the recovery instruction, not the
        # kubectl failure that happens to be last.
        raise SlackApiError(SLACK_ROTATION_LOST) from None

    def delete_app(self, app_id: str) -> bool:
        config_token, _ = self._store.read()
        return self._delete_app(app_id, config_token, retried=False)

    def _delete_app(self, app_id: str, config_token: str, *, retried: bool) -> bool:
        resp = self._client.post(
            f"{_SLACK_API_BASE}/apps.manifest.delete",
            data={"app_id": app_id},
            headers={"Authorization": f"Bearer {config_token}"},
        )
        body = resp.json()
        if body.get("ok"):
            return True

        error = body.get("error", "unknown_error")
        if error in _SLACK_AUTH_ERRORS and not retried:
            new_config_token = self._rotate(config_token)
            return self._delete_app(app_id, new_config_token, retried=True)
        if error in _SLACK_ALREADY_GONE_ERRORS:
            return False

        # Never include `app_id` or any token material here — just Slack's
        # own error code, same discipline as _create_app's raise.
        raise SlackApiError(f"slack app deletion failed: {error}")


# --- Image (Vertex AI Model Garden avatar PNGs) ------------------------------


@runtime_checkable
class ImageClient(Protocol):
    def generate(self, prompt: str) -> bytes:
        """Generate one square PNG from `prompt`; raise ClientError on failure."""
        ...


# The single user-visible failure message for every ImageClient.generate
# failure mode (safety-blocked/no-image 200, non-200, malformed JSON, bad
# base64) — deliberately generic and never built from the prompt or from
# server response bodies, since routes build request bodies straight from
# raised messages (see VertexImageClient.generate below for what DOES get
# logged, prompt included, server-side only).
_IMAGE_GENERATION_FAILED = "the picture didn't come through. Try again."


def _gcloud_print_access_token(runner: CommandRunner | None = None) -> str:
    """Default `token_provider` for VertexImageClient: shells `gcloud auth
    print-access-token` via CommandRunner and returns its (already-stripped
    by CommandRunner.run) stdout.

    No existing google-auth idiom exists anywhere in this codebase (checked
    kobo_api and the wider repo for `print-access-token`/`google.auth`/
    `google.oauth2`) — this mirrors the CLI-shell-out idiom every other real
    client in this module already uses (GhCLI, GcloudClient, KubectlCLI) for
    exactly the same account/credential surface the caller's `gcloud` CLI is
    already authenticated with, rather than adding a new google-auth library
    dependency for this one call.
    """
    return (runner or CommandRunner()).run(["gcloud", "auth", "print-access-token"])


# The GKE/GCE metadata server's token endpoint. Unlike `gcloud auth
# print-access-token`, it reports `expires_in` -- the token's REAL remaining
# lifetime -- which is the only way a cache can avoid outliving the token it
# holds (see CachedGcloudToken's docstring for the production regression this
# closes).
_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)
# Refresh this far ahead of a token's stated expiry.
_TOKEN_EXPIRY_MARGIN_S = 300
# How long a token whose real lifetime we could NOT learn may be trusted.
# Deliberately short: the gcloud fallback can hand back a token with only
# minutes left, and assuming otherwise is exactly what broke the room.
_UNKNOWN_LIFETIME_TTL_S = 300
# Never cache for less than this, so a token near expiry cannot turn every
# call into a fetch.
_MIN_TOKEN_TTL_S = 30


def _metadata_access_token(client: httpx.Client | None = None) -> tuple[str, int] | None:
    """`(token, seconds-remaining)` from the metadata server, or None when it
    is not reachable or does not answer usefully (any local dev machine, or a
    pod without Workload Identity). Never raises: a None answer simply means
    "ask gcloud instead", and the caller's own failure handling covers the
    rest."""
    try:
        resp = (client or httpx).get(
            _METADATA_TOKEN_URL,
            headers={"Metadata-Flavor": "Google"},
            timeout=5.0,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return str(data["access_token"]), int(data["expires_in"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def _google_access_token() -> tuple[str, int] | str:
    """The room's default access-token source: the metadata server when it
    answers (returning the real remaining lifetime), otherwise the gcloud
    shell-out (returning a bare token of unknown lifetime). In the deployed
    room this also removes the 1-2s shell-out entirely."""
    from_metadata = _metadata_access_token()
    if from_metadata is not None:
        return from_metadata
    return _gcloud_print_access_token()


class CachedGcloudToken:
    """Caches `_gcloud_print_access_token`'s result for `ttl_s` seconds.

    Every real client in this module that talks to a Google API
    (`VertexImageClient`, `VertexNameClient`, `HttpxStorageClient`) defaults
    its `token_provider` to `_gcloud_print_access_token`, which shells out to
    `gcloud auth print-access-token` — measured ~1-2s per call in the
    deployed pod. A Google access token is good for about an hour, so paying
    that shell-out fresh on every single Vertex/GCS call (per-thought, in
    the deployed room) is pure waste. `Clients.real` wires ONE instance of
    this class into all three clients so a token minted for one is reused by
    the others until it goes stale.

    `ttl_s` (default 1800s) is a CEILING, not the window itself. It used to be
    the whole story, on the assumption that every fetch returned a fresh
    hour-long token. That is false, and it broke the room on 2026-08-24:
    `gcloud auth print-access-token` returns whatever gcloud already has
    cached, so a fetch can hand back a token with only minutes left. Pinning
    that for a fixed 1800s meant the room served an EXPIRED bearer token and
    every GCS call answered 401 Invalid Credentials until the window closed,
    while the catalog quietly went on serving a stale table. So: when the
    source reports a real remaining lifetime (the metadata server does), the
    window is that lifetime minus `_TOKEN_EXPIRY_MARGIN_S`, capped by
    `ttl_s`; when it does not (the gcloud fallback), the window is the short
    `_UNKNOWN_LIFETIME_TTL_S` rather than an optimistic guess.

    Thread-safe (`threading.Lock`, held across the fetch itself, not just a
    read-then-write of the cached fields): background threads that also
    call Vertex (the persona draft/distill/capability seams,
    `kobo_api.persona_draft`) must never race into two overlapping
    `gcloud` shell-outs, and concurrent callers hitting a cold cache
    together must still cost exactly one fetch.

    A fetch failure is never cached and never wipes an already-cached,
    unexpired token: the lock is only ever held while EITHER returning the
    still-fresh cached token OR calling `fetch()`, so a raised exception
    from `fetch()` propagates straight through (the same `ClientError`
    shape every caller already handles) without this class touching
    `_token`/`_expires_at` at all — a later call simply retries.

    `fetch` may return either `(token, seconds-remaining)` or a bare token
    string; `fetch` and `now` are test seams only (the real
    `_google_access_token` and `time.monotonic` by default), mirroring `InstallationTokenProvider`'s
    own cache/expiry-margin/`now` seam discipline (`kobo_api.github_app`).
    `time.monotonic` (not `time.time`) is deliberate: this cache only ever
    compares against its own earlier reading, so it must be immune to
    wall-clock adjustments (NTP step, DST, an operator setting the clock)
    that `time.time` is exposed to.
    """

    def __init__(
        self,
        fetch: Callable[[], tuple[str, int] | str] = _google_access_token,
        ttl_s: int = 1800,
        now: Callable[[], float] = time.monotonic,
    ):
        self._fetch = fetch
        self._ttl_s = ttl_s
        self._now = now
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float | None = None

    def __call__(self) -> str:
        with self._lock:
            if (
                self._token is not None
                and self._expires_at is not None
                and self._now() < self._expires_at
            ):
                return self._token
            fetched = self._fetch()
            if isinstance(fetched, tuple):
                # The source told us the token's REAL remaining lifetime:
                # never hold it past that, and keep `ttl_s` as the ceiling so
                # a long-lived token still costs one fetch per window.
                token, expires_in = fetched
                ttl = min(
                    self._ttl_s,
                    max(int(expires_in) - _TOKEN_EXPIRY_MARGIN_S, _MIN_TOKEN_TTL_S),
                )
            else:
                # Unknown lifetime (the gcloud fallback): stay conservative.
                token = fetched
                ttl = min(self._ttl_s, _UNKNOWN_LIFETIME_TTL_S)
            self._token = token
            self._expires_at = self._now() + ttl
            return token


class VertexImageClient:
    """Generates one square avatar PNG per prompt via Vertex AI's Model
    Garden `generateContent` endpoint (`gemini-2.5-flash-image` by default —
    see `Config.avatar_model`/`avatar_location`), Gemini's image-generation
    successor to the now-deprecated Imagen `:predict` endpoints (P2d
    discovery).

    A 200 response doesn't guarantee an image: the model can reply with a
    safety-blocked or text-only turn (no `inlineData` part anywhere), which
    is a normal outcome, not a transport error — mapped to the same
    `ClientError` as an actual failure so every caller has exactly one
    failure path to handle. Every failure mode below raises the identical
    plain `_IMAGE_GENERATION_FAILED` message; the prompt itself is logged
    (server-side only, at warning level) for operators debugging repeated
    failures, but is never part of a raised exception's message — routes
    build their response bodies straight from those messages.
    """

    def __init__(
        self,
        project: str,
        location: str,
        model: str,
        http_client: httpx.Client | None = None,
        token_provider: Callable[[], str] | None = None,
    ):
        self._project = project
        self._location = location
        self._model = model
        # Image generation is a full model roundtrip (like KubectlAgentClient's
        # a2a hello, routinely slow) — same generous headroom over httpx's
        # bare 5s default, not a quick CRUD call's timeout.
        self._http_client = http_client or httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))
        self._token_provider = token_provider or _gcloud_print_access_token

    def generate(self, prompt: str) -> bytes:
        url = (
            f"https://{self._location}-aiplatform.googleapis.com/v1/projects/"
            f"{self._project}/locations/{self._location}/publishers/google/"
            f"models/{self._model}:generateContent"
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        try:
            token = self._token_provider()
        except ClientError as exc:
            # The default token_provider shells `gcloud auth
            # print-access-token` via CommandRunner, whose ClientError
            # message embeds the full command line + stderr (possibly the
            # operator's account email) — left unwrapped, that would escape
            # generate() and reach the avatar route's 502 body verbatim.
            # Log the real error server-side only, and re-raise the same
            # plain message every other failure mode here raises.
            log.warning("token provider failed for prompt %r: %r", prompt, exc)
            raise ClientError(_IMAGE_GENERATION_FAILED) from exc

        try:
            resp = self._http_client.post(
                url, json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            log.warning("image generation request failed for prompt %r: %r", prompt, exc)
            raise ClientError(_IMAGE_GENERATION_FAILED) from exc

        if resp.status_code != 200:
            log.warning(
                "image generation failed (%s) for prompt %r: %s",
                resp.status_code, prompt, resp.text[:200],
            )
            raise ClientError(_IMAGE_GENERATION_FAILED)

        try:
            parts = resp.json()["candidates"][0]["content"]["parts"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("malformed image generation reply for prompt %r: %r", prompt, exc)
            raise ClientError(_IMAGE_GENERATION_FAILED) from exc

        for part in parts:
            inline_data = part.get("inlineData")
            if not inline_data:
                continue
            try:
                return base64.b64decode(inline_data["data"])
            except (KeyError, ValueError, TypeError) as exc:
                log.warning("malformed inlineData for prompt %r: %r", prompt, exc)
                raise ClientError(_IMAGE_GENERATION_FAILED) from exc

        # No inlineData part in any reply part at all — safety-blocked or a
        # text-only turn. A normal, expected outcome, not a transport error.
        log.warning("no image part in generation reply for prompt %r", prompt)
        raise ClientError(_IMAGE_GENERATION_FAILED)


# --- Name (Vertex AI text generateContent: agent name suggestions) ----------------


@runtime_checkable
class NameClient(Protocol):
    def suggest(
        self,
        who: str,
        what: str,
        *,
        tags: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> list[dict]:
        """Ask the generator for name+reason candidates for an agent
        described by `who` (who_they_are) and `what` (what_they_do); raise
        ClientError on a transport/envelope/parse failure.

        `tags` (create-identity, Task 2) are the persona's purpose tags,
        when a persona_id resolved server-side (kobo_api.main's
        name-suggestions route) — steer the persona-mining prompt toward
        this persona's themes; `exclude` (same task) is names already
        offered this session (an earlier round's suggestions), so a repeat
        "offer more" call doesn't resurface them. Both optional and purely
        advisory to the prompt; neither is validated here.

        Not guaranteed to return exactly three (or even any) usable
        candidates — `kobo_api.namegen.finalize_suggestions` is what filters
        and pads the result to exactly three; this method's only job is
        best-effort extraction of whatever candidates the model's reply
        actually contained.
        """
        ...

    def draft(self, job: str, rules: list[str]) -> dict:
        """Ask the generator to draft a persona (Persona v3 addendum) from
        one plain `job` description plus optional `rules` hints; raise
        ClientError on a transport/envelope/parse failure.

        Returns a `{"name", "who_they_are", "what_they_do"}` dict whose
        values are whatever the model's reply actually contained for each
        key (`None` for one that's missing, or whatever raw JSON type it
        was — no type/bounds checking here). Not guaranteed that any field
        is actually usable — `kobo_api.persona_draft.finalize_draft` is
        what validates each field against PersonaFields' real bounds and
        falls each one back individually; this method's only job is
        best-effort structural extraction of whatever the model's reply
        actually contained. Same "one client, one method per text-
        generation shape" split as `suggest` above — a single
        `VertexNameClient` implements both, sharing the same token seam and
        model config, since a persona draft is just a different-shaped
        Vertex text call, not a different backend.
        """
        ...

    def capability(
        self, persona_body: str, skill_descriptions: list[str], tooling_filenames: list[str]
    ) -> str:
        """Ask the generator for one plain sentence describing what an
        agent with this persona (plus its attached skills/tooling) can
        actually do, for its directory card (persona-metadata plan, Task 3);
        raise ClientError on a transport/envelope/parse failure.

        Returns whatever plain-text reply the model actually gave — no
        length/punctuation enforcement here (mirrors `suggest`/`draft`'s own
        "no type/bounds checking" split): `kobo_api.metadata.
        generate_capability` is what clamps the reply to
        `CAPABILITY_MAX_LEN` chars and strips em/en dashes defensively, and
        what falls back to a deterministic tier on ANY failure here — a
        raised ClientError from this method must never block an agent's
        readiness. Same "one client, one method per text-generation shape"
        split as `suggest`/`draft` above.
        """
        ...

    def tags(self, persona_body: str, capability: str, group: str | None) -> list[str]:
        """Ask the generator for a handful of short purpose tags for an agent
        that has just made first contact (2026-09-29: agents tag themselves;
        the create form no longer asks). Raise ClientError on a
        transport/envelope/parse failure. `kobo_api.metadata.generate_tags`
        normalizes and caps the reply and falls back to no tags on any
        failure, so nothing here can block readiness.
        """
        ...

    def distill(self, body: str) -> dict:
        """Ask the generator to read a ready-made persona document (`body`,
        the operator's own upload/paste) and pull out discovery metadata for
        the colleague directory (persona-first station addendum): a name,
        a one-line who-they-are, and purpose tags. Raise ClientError on a
        transport/envelope/parse failure.

        Returns a `{"name", "who_they_are", "purpose_tags"}` dict whose
        values are whatever the model's reply actually contained for each
        key (`None` for one that's missing, or whatever raw JSON type it
        was — no type/bounds checking here). Deliberately no
        `what_they_do` key at all: `body` itself already IS the persona,
        so there's nothing to draft for that field — this method only ever
        pulls metadata OUT of it. Not guaranteed that any field is
        actually usable — `kobo_api.persona_draft.finalize_distill` is what
        validates each field against PersonaFields' real bounds and falls
        each one back individually; this method's only job is best-effort
        structural extraction of whatever the model's reply actually
        contained. Same "one client, one method per text-generation shape"
        split as `suggest`/`draft`/`capability` above.
        """
        ...


# The single server-side-only failure message for every VertexNameClient.
# suggest failure mode (non-200, malformed envelope, unparsable text) —
# never built from the prompt or a raw response body, mirroring
# _IMAGE_GENERATION_FAILED's convention above. Unlike the avatar route (which
# 502s straight from this message), the name-suggestions route never shows
# this to a caller at all — a ClientError here just means "fall through to
# the curated pool" (kobo_api.main's name_suggestions route), so this string
# only ever reaches a log line.
_NAME_SUGGESTION_FAILED = "name suggestion generation failed"

# Tolerant extraction: finds the first `[...]` span anywhere in a reply's
# text (past a chatty preamble, a ```json fence, trailing commentary, ...)
# rather than assuming the array starts at index 0 or is the whole string.
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _extract_json_array(text: str) -> list:
    """Parse the first `[...]` span found in `text` as JSON.

    Raises `ValueError` when no such span exists at all (a plain-prose
    refusal, say) — `json.loads` raising `json.JSONDecodeError` (itself a
    `ValueError` subclass) on a span that isn't valid JSON is left
    unwrapped, since both are exactly the same "unusable reply" case to
    every caller of this function.
    """
    match = _JSON_ARRAY_RE.search(text)
    if not match:
        raise ValueError("no JSON array found in response text")
    return json.loads(match.group(0))


# Same tolerant-extraction idiom as _JSON_ARRAY_RE/_extract_json_array above,
# just for the persona-draft reply shape (one JSON object) rather than the
# name-suggestions one (a JSON array of candidates).
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(text: str) -> dict:
    """Parse the first `{...}` span found in `text` as JSON.

    Raises `ValueError` when no such span exists at all — same "unusable
    reply" contract as `_extract_json_array` above (a `json.JSONDecodeError`
    on a span that isn't valid JSON is left unwrapped for the same reason:
    both are exactly the same case to every caller).
    """
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        raise ValueError("no JSON object found in response text")
    return json.loads(match.group(0))


def _render_name_prompt_list(items: list[str] | None, when_empty: str) -> str:
    """Render a `tags`/`exclude` list for embedding in
    `_NAME_PROMPT_TEMPLATE`: comma-joined when non-empty, else the literal
    `when_empty` placeholder text — never the string "None" and never a
    dangling empty span, whether `items` is `None` or an empty list.
    """
    if not items:
        return when_empty
    return ", ".join(items)


# Persona-mining prompt (create-identity, Task 2): unlike the old fixed
# "craft workshop's bench... quiet trades" theme (which named the same
# lexical universe regardless of persona), this prompt draws candidate
# names OUT of the persona actually passed in — `who`/`what`/`tags` — and
# steers toward whatever world that persona evokes (a public figure, a
# fictional character, a team) before falling back to the persona's own
# domain vocabulary. `exclude` keeps repeat "offer more" rounds from
# resurfacing names already shown. Spec: 2026-09-02-create-identity-and-
# mcp-currency-design.md, section 1, "Cause 2".
_NAME_PROMPT_TEMPLATE = (
    "You are naming a software agent. Its persona: {who}. What it does: "
    "{what}. Its themes: {tags}.\n\n"
    "Suggest exactly three short, lowercase, single-word candidate names "
    "that RESONATE with this persona's character and domain. If the "
    "persona evokes a public figure, a fictional character, a team, or a "
    "vivid world, draw the names from that world -- nicknames, associated "
    "places, terms of art. Otherwise draw from the persona's own domain "
    "vocabulary. Rules: each name is one word, lowercase letters/digits/"
    "hyphens only, starting with a letter; never an employer's or "
    "client's company name; never any of: {exclude}. For each, give a "
    "reason of 8 words or fewer tying it to this persona.\n\n"
    'Respond with ONLY a strict JSON array of exactly three objects shaped '
    '{{"name": "...", "reason": "..."}} -- no prose, no markdown fences, '
    "nothing before or after the array."
)

# The single server-side-only failure message for every
# VertexNameClient.draft failure mode (Persona v3 addendum) — same
# generic-message convention as _NAME_SUGGESTION_FAILED above: never built
# from the job text or a raw response body, and (also like the
# name-suggestions route) never shown to a caller at all — the persona-draft
# route always has a deterministic fallback to fall through to
# (`kobo_api.persona_draft.finalize_draft`), so this string only ever
# reaches a log line.
_PERSONA_DRAFT_FAILED = "persona draft generation failed"


def _build_persona_draft_prompt(job: str, rules: list[str]) -> str:
    """Build the persona-draft prompt from `job` (free-form, 1-500 chars)
    and optional `rules` hints.

    Deliberately plain string concatenation, NEVER `str.format()` against
    `job`/`rules` — the exact same hazard `kobo_api.personas.render_persona`
    documents avoiding: `job` is free-form user text that may itself
    contain literal "{"/"}" (e.g. describing a templating task), and
    running that through `.format()` would raise (or silently
    misinterpret). Unlike `_NAME_PROMPT_TEMPLATE` above (whose `who`/`what`
    go through `.format()`), this prompt is built without it.
    """
    rules_block = ""
    if rules:
        bullet_lines = "\n".join(f"- {rule}" for rule in rules)
        rules_block = "\nAlways/never rules to honor:\n" + bullet_lines + "\n"
    return (
        "You are drafting a persona for a small software agent at a craft "
        "workshop's bench, from one plain description of the job it should "
        "do.\n\n"
        "The job: " + job + "\n"
        + rules_block + "\n"
        "Write, in the house voice — the persona always addresses the "
        'agent as "you", never first person — exactly:\n'
        "- a SHORT, memorable persona name: one or two words, lowercase, "
        "slug-valid (hyphenate a two-word name), the shape of a "
        "craftsman's nickname — sentinel, ledger-keeper. NEVER a "
        "hyphenated list of traits, and never a client's or company's "
        "own name; the description carries the traits, not the name\n"
        "- ONE vivid who-they-are line, 200 characters or fewer, a single "
        "line with no line breaks — this line IS where the persona's "
        "traits live. Write it as a noun phrase that completes the "
        "sentence 'You are NAME, ...' (e.g. 'a meticulous auditor who "
        "cites the exact clause before judging'); it must NEVER itself "
        "begin with 'You are'\n"
        "- what_they_do: a FULL working brief in markdown, roughly 40 to "
        "120 lines — how you work (as '## How you work'), what you "
        "refuse or escalate (as '## Boundaries'), and a few concrete "
        "example asks with how you'd answer (as '## What people ask "
        "you'). Write it with the depth of a real handbook page for a "
        "new colleague, never a summary paragraph\n"
        "- 1-5 short, lowercase purpose_tags (one or two words each) "
        "capturing what this persona is for — plain domain words someone "
        "would search, never abstractions\n\n"
        'Respond with ONLY a strict JSON object shaped {"name": "...", '
        '"who_they_are": "...", "what_they_do": "...", "purpose_tags": '
        '["...", ...]} — no prose, no markdown fences, no commentary, '
        "nothing before or after the object."
    )


# The single server-side-only failure message for every
# VertexNameClient.capability failure mode (persona-metadata plan, Task 3) —
# same generic-message convention as _PERSONA_DRAFT_FAILED above: never built
# from the persona body or a raw response body, and never shown to a caller
# at all — kobo_api.metadata.generate_capability always has a deterministic
# fallback to fall through to, so this string only ever reaches a log line.
_CAPABILITY_FAILED = "capability generation failed"
_TAGS_FAILED = "tag generation failed"


def _build_tags_prompt(persona_body: str, capability: str, group: str | None) -> str:
    """The prompt behind `NameClient.tags`. Plain concatenation, never
    `str.format()` against the persona body (the brace hazard
    `_build_capability_prompt` documents)."""
    group_line = f"It works for the {group} team.\n" if group else ""
    return (
        "You are labelling a small software agent for a directory, so people "
        "and other agents can find it by what it is for.\n\n"
        "Its persona:\n" + persona_body + "\n\n"
        "What it can do, in one sentence:\n" + capability + "\n"
        + group_line
        + "\nRespond with ONLY a JSON array of 2 to 5 short lowercase tags, "
        "each one or two words joined by a hyphen, naming the work it does "
        "and the area it works in (for example [\"billing\", \"gcp\", "
        "\"cost-review\"]). No prose before or after the array."
    )


def _build_capability_prompt(
    persona_body: str, skill_descriptions: list[str], tooling_filenames: list[str]
) -> str:
    """Build the capability-generation prompt from a persona body plus its
    attached skills' descriptions and tooling filenames.

    Deliberately plain string concatenation, never `str.format()` against
    `persona_body` — same brace hazard `_build_persona_draft_prompt` already
    documents avoiding: a persona body is free-form text that may itself
    contain literal `{`/`}`.
    """
    skills_block = ""
    if skill_descriptions:
        bullet_lines = "\n".join(f"- {d}" for d in skill_descriptions)
        skills_block = "\nIt has these skills:\n" + bullet_lines + "\n"
    tooling_block = ""
    if tooling_filenames:
        bullet_lines = "\n".join(f"- {f}" for f in tooling_filenames)
        tooling_block = "\nIt has these tools available:\n" + bullet_lines + "\n"
    return (
        "You are describing, in ONE plain sentence, what a small software "
        "agent at a craft workshop's bench can actually do — for a "
        "directory card a visitor reads to decide whether to talk to it.\n\n"
        "Its persona:\n" + persona_body + "\n"
        + skills_block + tooling_block + "\n"
        "Respond with ONLY that one sentence — plain prose, 160 characters "
        "or fewer, no markdown, no quotes, no dashes of any kind, no "
        "preamble, nothing before or after the sentence."
    )


# The single server-side-only failure message for every
# VertexNameClient.distill failure mode (persona-first station addendum) —
# same generic-message convention as _PERSONA_DRAFT_FAILED/_CAPABILITY_FAILED
# above: never built from the persona document or a raw response body, and
# never shown to a caller at all — the distill route always has a
# deterministic fallback to fall through to
# (`kobo_api.persona_draft.finalize_distill`), so this string only ever
# reaches a log line.
_PERSONA_DISTILL_FAILED = "persona distill generation failed"


def _build_persona_distill_prompt(body: str) -> str:
    """Build the persona-distill prompt from `body` (the operator's own
    ready-made persona document, 1-49152 chars).

    Deliberately plain string concatenation, never `str.format()` against
    `body` — same brace hazard `_build_persona_draft_prompt` already
    documents avoiding: `body` is free-form operator text that may itself
    contain literal "{"/"}".
    """
    return (
        "You are reading a persona document already written for a small "
        "software agent at a craft workshop's bench, and pulling out "
        "discovery metadata other colleagues will use to find this agent "
        "in a directory.\n\n"
        "The persona document:\n" + body + "\n\n"
        "From it, write exactly:\n"
        "- a SHORT persona name: one or two words, lowercase, slug-valid "
        "(hyphenate a two-word name), the shape of a craftsman's nickname. "
        "NEVER a hyphenated list of traits, and never a client's or "
        "company's own name\n"
        "- ONE plain who-they-are sentence, 200 characters or fewer, a "
        "single line with no line breaks\n"
        "- 1-5 short, lowercase purpose_tags (one or two words each) "
        "capturing what this persona is for\n\n"
        'Respond with ONLY a strict JSON object shaped {"name": "...", '
        '"who_they_are": "...", "purpose_tags": ["...", ...]}, no prose, '
        "no markdown fences, no commentary, nothing before or after the "
        "object."
    )


class VertexNameClient:
    """Generates name+reason suggestion candidates via Vertex AI's Model
    Garden text `generateContent` endpoint (`gemini-2.5-flash` by default —
    see `Config.name_model`/`name_location`) — same gcloud-shelled bearer
    token + auth/transport family as `VertexImageClient`
    (`_gcloud_print_access_token`), just a plain TEXT call instead of an
    image one (no `generationConfig.responseModalities` override needed —
    Vertex's text default already models the desired output shape).
    `suggest()` below does send its own `generationConfig` regardless (see
    its own comment) to turn dynamic thinking off, since a naming task needs
    none of it, and to cap the output length; `draft()`/`capability()`/
    `distill()` further down send none.

    A 200 response doesn't guarantee a well-formed answer: the model can
    reply with a chatty preamble, a fenced code block, or truncated JSON —
    parsing is deliberately tolerant (`_extract_json_array` above), and
    individual malformed items inside an otherwise-valid array are silently
    dropped rather than failing the whole call. Only a structural failure —
    non-200, a malformed response envelope, or no parseable JSON array at
    all — raises `ClientError`; the caller (`kobo_api.main`'s
    name-suggestions route) always has a curated fallback pool to fall
    through to, so a raised `ClientError` here is never fatal to the route,
    just to this one generator call. Response bodies are logged
    server-side only, truncated (same convention as
    `VertexImageClient.generate`), never as part of a raised message.
    """

    def __init__(
        self,
        project: str,
        location: str,
        model: str,
        http_client: httpx.Client | None = None,
        token_provider: Callable[[], str] | None = None,
    ):
        self._project = project
        self._location = location
        self._model = model
        # A text completion is a much lighter roundtrip than image
        # generation but still a full model call — real headroom over
        # httpx's bare 5s default, short of VertexImageClient's generous
        # 120s (no image payload to wait on here).
        self._http_client = http_client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._token_provider = token_provider or _gcloud_print_access_token

    def suggest(
        self,
        who: str,
        what: str,
        *,
        tags: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> list[dict]:
        url = (
            f"https://{self._location}-aiplatform.googleapis.com/v1/projects/"
            f"{self._project}/locations/{self._location}/publishers/google/"
            f"models/{self._model}:generateContent"
        )
        prompt = _NAME_PROMPT_TEMPLATE.format(
            who=who,
            what=what,
            tags=_render_name_prompt_list(tags, "(none)"),
            exclude=_render_name_prompt_list(exclude, "(none offered yet)"),
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            # Naming is a taste task, not a reasoning task: gemini-2.5-flash
            # defaults to dynamic thinking whenever generationConfig omits
            # it, and that thinking budget is what was dominating latency
            # here, so thinking off keeps the ceremony snappy. 512 tokens is
            # well more than three names plus their one-line reasons ever
            # need.
            "generationConfig": {
                "thinkingConfig": {"thinkingBudget": 0},
                "maxOutputTokens": 512,
                "temperature": 1.0,
            },
        }

        try:
            token = self._token_provider()
        except ClientError as exc:
            # Same reasoning as VertexImageClient.generate: the default
            # token_provider's ClientError message can embed the operator's
            # own account details — never let it escape unwrapped.
            log.warning("token provider failed for name suggestions: %r", exc)
            raise ClientError(_NAME_SUGGESTION_FAILED) from exc

        try:
            resp = self._http_client.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            log.warning("name suggestion request failed: %r", exc)
            raise ClientError(_NAME_SUGGESTION_FAILED) from exc

        if resp.status_code != 200:
            log.warning(
                "name suggestion failed (%s): %s", resp.status_code, resp.text[:200]
            )
            raise ClientError(_NAME_SUGGESTION_FAILED)

        try:
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("malformed name suggestion reply: %r (body: %s)", exc, resp.text[:200])
            raise ClientError(_NAME_SUGGESTION_FAILED) from exc

        try:
            parsed = _extract_json_array(text)
        except ValueError as exc:
            log.warning("unparsable name suggestion text: %r (text: %s)", exc, text[:200])
            raise ClientError(_NAME_SUGGESTION_FAILED) from exc

        if not isinstance(parsed, list):
            log.warning(
                "name suggestion JSON wasn't an array (got %s)", type(parsed).__name__
            )
            raise ClientError(_NAME_SUGGESTION_FAILED)

        candidates: list[dict] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str):
                continue
            reason = item.get("reason")
            candidates.append({"name": name, "reason": reason if isinstance(reason, str) else ""})
        return candidates

    def draft(self, job: str, rules: list[str]) -> dict:
        url = (
            f"https://{self._location}-aiplatform.googleapis.com/v1/projects/"
            f"{self._project}/locations/{self._location}/publishers/google/"
            f"models/{self._model}:generateContent"
        )
        prompt = _build_persona_draft_prompt(job, rules)
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}

        try:
            token = self._token_provider()
        except ClientError as exc:
            # Same reasoning as suggest() above: the default token_provider's
            # ClientError message can embed the operator's own account
            # details — never let it escape unwrapped.
            log.warning("token provider failed for persona draft: %r", exc)
            raise ClientError(_PERSONA_DRAFT_FAILED) from exc

        try:
            resp = self._http_client.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            log.warning("persona draft request failed: %r", exc)
            raise ClientError(_PERSONA_DRAFT_FAILED) from exc

        if resp.status_code != 200:
            log.warning(
                "persona draft failed (%s): %s", resp.status_code, resp.text[:200]
            )
            raise ClientError(_PERSONA_DRAFT_FAILED)

        try:
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning(
                "malformed persona draft reply: %r (body: %s)", exc, resp.text[:200]
            )
            raise ClientError(_PERSONA_DRAFT_FAILED) from exc

        try:
            parsed = _extract_json_object(text)
        except ValueError as exc:
            log.warning(
                "unparsable persona draft text: %r (text: %s)", exc, text[:200]
            )
            raise ClientError(_PERSONA_DRAFT_FAILED) from exc

        if not isinstance(parsed, dict):
            log.warning(
                "persona draft JSON wasn't an object (got %s)", type(parsed).__name__
            )
            raise ClientError(_PERSONA_DRAFT_FAILED)

        # No type/bounds checking here (mirrors suggest()'s own split with
        # kobo_api.namegen.finalize_suggestions) — kobo_api.persona_draft.
        # finalize_draft is what validates each field against PersonaFields'
        # real bounds and falls each one back individually. purpose_tags is
        # the same story: whatever raw JSON value (or None, if the key's
        # missing) the reply carried, passed through untouched —
        # finalize_draft normalizes/caps it (or falls back to the persona's
        # own derived tags) on the way out.
        return {
            "name": parsed.get("name"),
            "who_they_are": parsed.get("who_they_are"),
            "what_they_do": parsed.get("what_they_do"),
            "purpose_tags": parsed.get("purpose_tags"),
        }

    def capability(
        self, persona_body: str, skill_descriptions: list[str], tooling_filenames: list[str]
    ) -> str:
        url = (
            f"https://{self._location}-aiplatform.googleapis.com/v1/projects/"
            f"{self._project}/locations/{self._location}/publishers/google/"
            f"models/{self._model}:generateContent"
        )
        prompt = _build_capability_prompt(persona_body, skill_descriptions, tooling_filenames)
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}

        try:
            token = self._token_provider()
        except ClientError as exc:
            # Same reasoning as suggest()/draft() above: the default
            # token_provider's ClientError message can embed the operator's
            # own account details — never let it escape unwrapped.
            log.warning("token provider failed for capability generation: %r", exc)
            raise ClientError(_CAPABILITY_FAILED) from exc

        try:
            resp = self._http_client.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            log.warning("capability generation request failed: %r", exc)
            raise ClientError(_CAPABILITY_FAILED) from exc

        if resp.status_code != 200:
            log.warning(
                "capability generation failed (%s): %s", resp.status_code, resp.text[:200]
            )
            raise ClientError(_CAPABILITY_FAILED)

        try:
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning(
                "malformed capability generation reply: %r (body: %s)", exc, resp.text[:200]
            )
            raise ClientError(_CAPABILITY_FAILED) from exc

        if not isinstance(text, str) or not text.strip():
            log.warning("capability generation reply had no usable text")
            raise ClientError(_CAPABILITY_FAILED)

        # No length/punctuation enforcement here (mirrors suggest()/draft()'s
        # own "no type/bounds checking" split) — kobo_api.metadata.
        # generate_capability is what clamps to CAPABILITY_MAX_LEN and strips
        # em/en dashes defensively.
        return text.strip()

    def tags(self, persona_body: str, capability: str, group: str | None) -> list[str]:
        url = (
            f"https://{self._location}-aiplatform.googleapis.com/v1/projects/"
            f"{self._project}/locations/{self._location}/publishers/google/"
            f"models/{self._model}:generateContent"
        )
        prompt = _build_tags_prompt(persona_body, capability, group)
        request_body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        try:
            token = self._token_provider()
        except ClientError as exc:
            log.warning("token provider failed for tag generation: %r", exc)
            raise ClientError(_TAGS_FAILED) from exc
        try:
            resp = self._http_client.post(
                url, json=request_body, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            log.warning("tag generation request failed: %r", exc)
            raise ClientError(_TAGS_FAILED) from exc
        if resp.status_code != 200:
            log.warning("tag generation failed (%s): %s", resp.status_code, resp.text[:200])
            raise ClientError(_TAGS_FAILED)
        try:
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            parsed = _extract_json_array(str(text))
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("malformed tag generation reply: %r (body: %s)", exc, resp.text[:200])
            raise ClientError(_TAGS_FAILED) from exc
        return [item for item in parsed if isinstance(item, str)]

    def distill(self, body: str) -> dict:
        url = (
            f"https://{self._location}-aiplatform.googleapis.com/v1/projects/"
            f"{self._project}/locations/{self._location}/publishers/google/"
            f"models/{self._model}:generateContent"
        )
        prompt = _build_persona_distill_prompt(body)
        # Named `request_body` (not `body`, unlike suggest()/draft()/
        # capability()'s own local payload var) — this method's own
        # parameter is already named `body` (the persona document), so
        # reusing that name for the request envelope here would shadow it.
        request_body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}

        try:
            token = self._token_provider()
        except ClientError as exc:
            # Same reasoning as suggest()/draft()/capability() above: the
            # default token_provider's ClientError message can embed the
            # operator's own account details — never let it escape
            # unwrapped.
            log.warning("token provider failed for persona distill: %r", exc)
            raise ClientError(_PERSONA_DISTILL_FAILED) from exc

        try:
            resp = self._http_client.post(
                url, json=request_body, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            log.warning("persona distill request failed: %r", exc)
            raise ClientError(_PERSONA_DISTILL_FAILED) from exc

        if resp.status_code != 200:
            log.warning(
                "persona distill failed (%s): %s", resp.status_code, resp.text[:200]
            )
            raise ClientError(_PERSONA_DISTILL_FAILED)

        try:
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning(
                "malformed persona distill reply: %r (body: %s)", exc, resp.text[:200]
            )
            raise ClientError(_PERSONA_DISTILL_FAILED) from exc

        try:
            parsed = _extract_json_object(text)
        except ValueError as exc:
            log.warning(
                "unparsable persona distill text: %r (text: %s)", exc, text[:200]
            )
            raise ClientError(_PERSONA_DISTILL_FAILED) from exc

        if not isinstance(parsed, dict):
            log.warning(
                "persona distill JSON wasn't an object (got %s)", type(parsed).__name__
            )
            raise ClientError(_PERSONA_DISTILL_FAILED)

        # No type/bounds checking here (mirrors draft()'s own split with
        # kobo_api.persona_draft.finalize_draft) — kobo_api.persona_draft.
        # finalize_distill is what validates each field against
        # PersonaFields' real bounds and falls each one back individually.
        # Deliberately no `what_they_do` key: `body` itself already IS the
        # persona, so there's nothing to draft for that field.
        return {
            "name": parsed.get("name"),
            "who_they_are": parsed.get("who_they_are"),
            "purpose_tags": parsed.get("purpose_tags"),
        }


# --- Storage (GCS JSON API: bucket-backed persistence for catalogs/personas) ------


@runtime_checkable
class StorageClient(Protocol):
    def read_bytes(self, bucket: str, key: str) -> bytes:
        """GET the object's raw bytes. 404 -> StorageNotFound; other non-200 -> ClientError."""
        ...

    def object_metadata(self, bucket: str, key: str) -> dict:
        """Full object resource JSON; custom metadata under result.get('metadata', {}),
        server fields like 'updated' at top level. 404 -> StorageNotFound."""
        ...

    def write_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        *,
        metadata: dict[str, str] | None = None,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Multipart-upload `data` to `bucket`/`key`, with optional custom metadata."""
        ...

    def list_keys(self, bucket: str, prefix: str) -> list[str]:
        """Object names under `prefix`, following pagination. [] when the
        bucket has no matching objects; bucket missing (404) -> StorageNotFound."""
        ...

    def ensure_bucket(self, bucket: str) -> str:
        """"exists" if `bucket` is already there; otherwise creates it (with
        versioning enabled) and returns "created"."""
        ...

    def ensure_versioning(self, bucket: str) -> bool:
        """Enable versioning on `bucket` if it isn't already — True if this
        call changed it, False if it was already on."""
        ...


_STORAGE_API_BASE = "https://storage.googleapis.com/storage/v1"
_STORAGE_UPLOAD_BASE = "https://storage.googleapis.com/upload/storage/v1"


class HttpxStorageClient:
    """GCS JSON API client, scoped to one GCP project.

    No google-cloud-storage SDK dependency — same shell-token-then-httpx
    idiom as VertexImageClient (no existing google-auth idiom exists
    anywhere in this codebase; see `_gcloud_print_access_token`), reused
    here as the default `token_provider` rather than reinventing it.

    Every failure mode raises a message built only from the method name and
    the HTTP status code — never the response body (which can carry a
    bucket's own error detail, or occasionally an operator's project info)
    and never the bearer token. The full response body is logged
    server-side (truncated) for operators debugging repeated failures, same
    convention as VertexImageClient.generate.
    """

    def __init__(
        self,
        project: str,
        http_client: httpx.Client | None = None,
        token_provider: Callable[[], str] | None = None,
    ):
        self._project = project
        # A quick JSON-API CRUD call, not a model roundtrip — real headroom
        # over httpx's bare 5s default, but nowhere near the generous
        # timeouts VertexImageClient/KubectlAgentClient need.
        self._http_client = http_client or httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0))
        self._token_provider = token_provider or _gcloud_print_access_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_provider()}"}

    def _object_url(self, bucket: str, key: str) -> str:
        return f"{_STORAGE_API_BASE}/b/{bucket}/o/{quote(key, safe='')}"

    def read_bytes(self, bucket: str, key: str) -> bytes:
        resp = self._http_client.get(
            self._object_url(bucket, key), params={"alt": "media"}, headers=self._headers()
        )
        if resp.status_code == 404:
            raise StorageNotFound(f"object not found: {bucket}/{key}")
        if resp.status_code != 200:
            log.warning(
                "read_bytes(%s, %s) failed (%s): %s",
                bucket, key, resp.status_code, resp.text[:200],
            )
            raise ClientError(f"read_bytes({bucket!r}, {key!r}) failed ({resp.status_code})")
        return resp.content

    def object_metadata(self, bucket: str, key: str) -> dict:
        resp = self._http_client.get(self._object_url(bucket, key), headers=self._headers())
        if resp.status_code == 404:
            raise StorageNotFound(f"object not found: {bucket}/{key}")
        if resp.status_code != 200:
            log.warning(
                "object_metadata(%s, %s) failed (%s): %s",
                bucket, key, resp.status_code, resp.text[:200],
            )
            raise ClientError(f"object_metadata({bucket!r}, {key!r}) failed ({resp.status_code})")
        return resp.json()

    def write_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        *,
        metadata: dict[str, str] | None = None,
        content_type: str = "application/octet-stream",
    ) -> None:
        object_resource: dict = {"name": key, "contentType": content_type}
        if metadata:
            object_resource["metadata"] = metadata

        # GCS's multipart upload wants a raw multipart/related body (JSON
        # object-resource part, then the media bytes part) — httpx has no
        # built-in for this shape (its `files=` helper builds multipart/
        # form-data, a different content type), so it's assembled by hand.
        boundary = f"kobo-{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        ).encode()
        body += json.dumps(object_resource).encode()
        body += f"\r\n--{boundary}\r\nContent-Type: {content_type}\r\n\r\n".encode()
        body += data
        body += f"\r\n--{boundary}--\r\n".encode()

        resp = self._http_client.post(
            f"{_STORAGE_UPLOAD_BASE}/b/{bucket}/o",
            params={"uploadType": "multipart"},
            content=body,
            headers={
                **self._headers(),
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
        )
        if resp.status_code not in (200, 201):
            log.warning(
                "write_bytes(%s, %s) failed (%s): %s",
                bucket, key, resp.status_code, resp.text[:200],
            )
            raise ClientError(f"write_bytes({bucket!r}, {key!r}) failed ({resp.status_code})")

    def list_keys(self, bucket: str, prefix: str) -> list[str]:
        url = f"{_STORAGE_API_BASE}/b/{bucket}/o"
        keys: list[str] = []
        page_token: str | None = None
        while True:
            params = {"prefix": prefix, "fields": "items(name),nextPageToken"}
            if page_token:
                params["pageToken"] = page_token
            resp = self._http_client.get(url, params=params, headers=self._headers())
            if resp.status_code == 404:
                raise StorageNotFound(f"bucket not found: {bucket}")
            if resp.status_code != 200:
                log.warning(
                    "list_keys(%s, %s) failed (%s): %s",
                    bucket, prefix, resp.status_code, resp.text[:200],
                )
                raise ClientError(f"list_keys({bucket!r}, {prefix!r}) failed ({resp.status_code})")
            body = resp.json()
            keys.extend(item["name"] for item in body.get("items", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                return keys

    def ensure_bucket(self, bucket: str) -> str:
        url = f"{_STORAGE_API_BASE}/b/{bucket}"
        resp = self._http_client.get(url, headers=self._headers())
        if resp.status_code == 200:
            return "exists"
        if resp.status_code != 404:
            log.warning(
                "ensure_bucket(%s) describe failed (%s): %s",
                bucket, resp.status_code, resp.text[:200],
            )
            raise ClientError(f"ensure_bucket({bucket!r}) failed ({resp.status_code})")

        create_resp = self._http_client.post(
            f"{_STORAGE_API_BASE}/b",
            params={"project": self._project},
            json={"name": bucket, "versioning": {"enabled": True}},
            headers=self._headers(),
        )
        if create_resp.status_code not in (200, 201):
            log.warning(
                "ensure_bucket(%s) create failed (%s): %s",
                bucket, create_resp.status_code, create_resp.text[:200],
            )
            raise ClientError(
                f"ensure_bucket({bucket!r}) create failed ({create_resp.status_code})"
            )
        return "created"

    def ensure_versioning(self, bucket: str) -> bool:
        url = f"{_STORAGE_API_BASE}/b/{bucket}"
        resp = self._http_client.get(url, params={"fields": "versioning"}, headers=self._headers())
        if resp.status_code == 404:
            raise StorageNotFound(f"bucket not found: {bucket}")
        if resp.status_code != 200:
            log.warning(
                "ensure_versioning(%s) describe failed (%s): %s",
                bucket, resp.status_code, resp.text[:200],
            )
            raise ClientError(f"ensure_versioning({bucket!r}) failed ({resp.status_code})")

        if resp.json().get("versioning", {}).get("enabled"):
            return False

        patch_resp = self._http_client.patch(
            url, json={"versioning": {"enabled": True}}, headers=self._headers()
        )
        if patch_resp.status_code != 200:
            log.warning(
                "ensure_versioning(%s) patch failed (%s): %s",
                bucket, patch_resp.status_code, patch_resp.text[:200],
            )
            raise ClientError(
                f"ensure_versioning({bucket!r}) patch failed ({patch_resp.status_code})"
            )
        return True


# --- Cabal (switchboard: agent directory registration + a2a task relay) -----------


# The operator token env var name — a single constant so `cabal_operator_
# token` and every future reader agree on it, rather than the literal
# string getting typed out again at some other call site and risking a typo
# that silently reads as "unset". Never an org.yaml key (ORG_KEYS,
# kobo_api.org): the operator token is a deployment secret, not something
# a client's org config repo should ever be able to set.
_CABAL_OPERATOR_TOKEN_ENV = "CABAL_OPERATOR_TOKEN"


def cabal_operator_token() -> str | None:
    """The switchboard operator token from its env var, or None when unset.

    Read fresh on every call (no caching) rather than snapshotted at import
    time, matching `installation_token_provider`'s own "always read live"
    posture — a token delivered to the process after import (a mounted
    Secret whose value changed under a restarted pod) is picked up the same
    way. Never logged; the caller (`CabalClient`) never puts it anywhere but
    the `Authorization` header either.
    """
    return os.environ.get(_CABAL_OPERATOR_TOKEN_ENV)


def cabal_enabled(config: Config) -> bool:
    """Whether the switchboard integration is wired: both org config keys
    (`cabal_url`, `agent_endpoint_template`) set AND the operator token
    present in the environment.

    A single shared truthiness helper (Task 2's `DirectoryStep`, Task 3's
    chat route, and any future caller) so "is the switchboard on" is
    answered identically everywhere, rather than each call site
    re-deriving the same three-way AND and risking one of them drifting —
    e.g. checking the two config keys but forgetting the env var, which
    would go on to build a `CabalClient` with no token to send.
    """
    return bool(
        config.cabal_url and config.agent_endpoint_template and cabal_operator_token()
    )


class CabalClient:
    """Talks to the switchboard (cabal)'s admin API: registers/delists an
    agent in its directory (`graduate`/`absorb`), relays a room chat's
    a2a task envelope through it (`relay`) instead of hitting the agent
    directly, mints a colleague-scoped token for an agent (`mint_
    colleague`, Task 3/B3) so it can ask its own colleagues questions, and
    reads the switchboard's own admin conversation surface (`list_
    conversations`/`get_conversation`/`stream_events`, Task 4/B4 "the
    watching window") so the room can show every colleague conversation as
    it happens, and pushes the room's own allow-pairs policy to the
    switchboard's dispatch gate (`put_dispatch_pairs`, colleagues
    switchboard plan Task C4) so cabal always enforces exactly what kobo
    currently thinks callers may reach.

    Every failure mode raises a message built only from the method name,
    the target persona name, and the HTTP status code — never the response
    body (logged server-side, truncated, for operators debugging repeated
    failures — same convention as `HttpxStorageClient`) and never the
    bearer token, since the whole point of this client existing is to hold
    that token; letting it leak into a raised message would be exactly the
    kind of thing every other client here is already careful to avoid
    (`GhCLI`'s `GH_TOKEN` env, `KubectlCLI.apply_secret`'s literals, ...).
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        async_client: httpx.AsyncClient | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        # An admin JSON-API call (graduate/absorb), not a model roundtrip —
        # same default headroom as HttpxGojiraClient/HttpxStorageClient.
        # relay() below overrides this per-call with a much longer timeout;
        # it alone waits on the far side's own model roundtrip.
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        # Only `stream_events` (below) ever touches this one — a genuinely
        # async client, not the sync one above, so that route's cancellation
        # on client disconnect actually propagates (fix round 1: the sync
        # generator this replaced blocked a worker thread past disconnect).
        # The default timeout here is irrelevant in practice: every call
        # site passes its own explicit `timeout=` override, same convention
        # as `relay`'s 180s override of `self._client`'s own 30s default.
        self._async_client = async_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0)
        )
        # `put_dispatch_pairs`'s own shrink-to-empty guard (below) needs to
        # remember what the LAST push from this very instance looked like --
        # there is nowhere else to keep that memory (no row in kobo's own
        # DB, nothing cabal's response echoes back), and "in-process memory
        # is fine" (Task C4 design) since every caller shares one `Clients.
        # cabal` instance per process. Starts `False`: a fresh client's
        # first-ever push, even an empty one, is "nothing to allow yet," not
        # a shrink from anything.
        self._last_dispatch_pairs_nonempty = False
        # The lost-sync reconciliation flag (final-review fix round,
        # HIGH-2b): True whenever this instance's LAST attempted push
        # failed (either a non-2xx `CabalError` below or a raw transport
        # `httpx.HTTPError` propagating straight out of the request call),
        # False once a push has actually succeeded. Lives right beside
        # `_last_dispatch_pairs_nonempty` above for the same reason -- both
        # are in-process-only memory of this client's own push history,
        # shared by every caller through the one `Clients.cabal` instance
        # per process, "in-process memory is fine" per the same Task C4
        # design this mirrors.
        #
        # Nothing in this class READS the flag to decide whether to push --
        # `put_dispatch_pairs` always pushes whatever it's handed, same as
        # today. What actually reconciles a lost sync is that every one of
        # kobo's four consent-change triggers (`kobo_api.pairs.sync_
        # dispatch_pairs_best_effort`) recomputes and pushes the FULL,
        # current `allowed_pairs` set unconditionally on every call -- so
        # the very next trigger to fire, even one whose OWN pairs delta is
        # a no-op, still re-sends the room's complete current policy and
        # heals whatever a previous failed push left stale. This flag exists
        # to make that "did the last push actually land" fact observable
        # (logging, `dispatch_pairs_dirty` below) for the boot-time sync and
        # the operator resync route, not to gate anything itself.
        self._dispatch_pairs_dirty = False

    @property
    def dispatch_pairs_dirty(self) -> bool:
        """True iff this instance's most recent `put_dispatch_pairs` call
        failed and no later call has since succeeded -- see the flag's own
        `__init__` comment for the full reconciliation story."""
        return self._dispatch_pairs_dirty

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def graduate(self, name: str, *, endpoint: str, audience: str, card: dict) -> None:
        """Register `name` in the switchboard's directory at `endpoint`,
        with `audience` as the a2a audience it will sign calls for and
        `card` as its full agent card. Raises `CabalError` on non-2xx.
        """
        resp = self._client.post(
            f"{self._base_url}/admin/persona/{name}/graduate",
            json={"endpoint": endpoint, "audience": audience, "agent_card_json": card},
            headers=self._headers(),
        )
        if not resp.is_success:
            log.warning(
                "cabal graduate(%s) failed (%s): %s", name, resp.status_code, resp.text[:200]
            )
            raise CabalError(f"graduate({name!r}) failed ({resp.status_code})")

    def revoke_colleague(self, name: str) -> None:
        """Refuse every colleague pass `name` was issued before now, on the
        switchboard (`POST /admin/token/revoke`, `sub=agent:<name>`). Called
        BEFORE `mint_colleague` on a re-wire so the pass being replaced
        stops working and the new one, minted after, is the only one that
        does; and on destroy so nothing the agent held keeps working.
        Raises `CabalError` on non-2xx; the response body is logged
        server-side (truncated), never echoed into the raised message.
        """
        resp = self._client.post(
            f"{self._base_url}/admin/token/revoke",
            json={"sub": f"agent:{name}"},
            headers=self._headers(),
        )
        if not resp.is_success:
            log.warning(
                "cabal revoke_colleague(%s) failed (%s): %s",
                name, resp.status_code, resp.text[:200],
            )
            raise CabalError(f"revoke_colleague({name!r}) failed ({resp.status_code})")

    def absorb(self, name: str) -> None:
        """Delist `name` from the switchboard's directory. A 404 (already
        delisted — a repeat destroy, or a persona the switchboard never
        had) is tolerated silently; any other non-2xx raises `CabalError`.
        """
        resp = self._client.post(
            f"{self._base_url}/admin/persona/{name}/absorb",
            json={},
            headers=self._headers(),
        )
        if resp.status_code == 404:
            return
        if not resp.is_success:
            log.warning(
                "cabal absorb(%s) failed (%s): %s", name, resp.status_code, resp.text[:200]
            )
            raise CabalError(f"absorb({name!r}) failed ({resp.status_code})")

    def relay(self, name: str, task_envelope: dict) -> dict:
        """Relay `task_envelope` (the same a2a task shape `_build_task_
        envelope` produces) to persona `name` through the switchboard;
        returns the parsed JSON response (the completed task — reply
        extraction is the caller's job, Task 3).

        180s timeout (not the client's default) — this waits on a real
        model roundtrip on the agent behind the switchboard, the same
        "curl -m 10" lesson `KubectlAgentClient`'s own generous timeouts
        already learned. Raises `CabalError` on non-2xx.
        """
        resp = self._client.post(
            f"{self._base_url}/a2a/tasks",
            json={"target": name, "task": task_envelope},
            headers=self._headers(),
            timeout=httpx.Timeout(180.0, connect=10.0),
        )
        if not resp.is_success:
            log.warning(
                "cabal relay(%s) failed (%s): %s", name, resp.status_code, resp.text[:200]
            )
            raise CabalError(f"relay({name!r}) failed ({resp.status_code})")
        return resp.json()

    def mint_colleague(self, name: str) -> str:
        """Mint a colleague-scoped switchboard token for agent `name` (Task
        3/B3: every agent is born with a colleague pass) — the operator-
        bearer-authenticated `POST /admin/token/mint-service` call, fixed
        request shape confirmed against the switchboard's own implementation:
        `sub` names the agent (`agent:<name>`, matching the `sub` a colleague
        token carries when it dispatches through the switchboard, per the
        colleague scope's own directory/re-mint contract), `scope` is
        `["colleague"]` (never `operator` — this mint must never hand an
        agent admin capability), `aud` is `cabal-core`, and `ttl_days` is a
        fixed 365 (a year-long pass; expiry is accepted as this feature's
        only revocation story, same as `deliver_pass`'s own docstring notes).

        Returns the minted `access_token` string. Raises `CabalError` on
        non-2xx, same convention as `graduate`/`absorb`/`relay` above: the
        response body is logged server-side (truncated) for operators
        debugging repeated failures, never echoed into the raised message.

        The minted token itself is never logged and never appears in any
        raised message — same discipline this client already holds its own
        bearer token to (see the class docstring): the whole point of this
        method is minting a NEW secret, so leaking it into a log line or an
        exception any caller might let escape to a route's response body
        would defeat that purpose as surely as leaking the operator token
        this client already guards.
        """
        resp = self._client.post(
            f"{self._base_url}/admin/token/mint-service",
            json={
                "sub": f"agent:{name}",
                "scope": ["colleague"],
                "aud": "cabal-core",
                "ttl_days": 365,
            },
            headers=self._headers(),
        )
        if not resp.is_success:
            log.warning(
                "cabal mint_colleague(%s) failed (%s): %s",
                name, resp.status_code, resp.text[:200],
            )
            raise CabalError(f"mint_colleague({name!r}) failed ({resp.status_code})")
        return resp.json()["access_token"]

    def put_dispatch_pairs(self, pairs: list[tuple[str, str]] | set[tuple[str, str]]) -> int:
        """Atomically replace the switchboard's entire dispatch allow-pairs
        set with `pairs` (`PUT /admin/dispatch-pairs`, colleagues switchboard
        plan Task C4) -- kobo is the policy owner, cabal just enforces
        whatever set this call last handed it; cabal's own side dedupes and
        never echoes the set back, only a `{"count": N}` tally of what it
        now holds, which is this method's return value (never re-parsed as
        pairs -- there is nothing to parse, and no caller needs to).

        `pairs` is sent as a sorted list (`sorted(pairs)`, tuples compare
        lexicographically) rather than in whatever order a `set` happened to
        iterate in -- determinism, so two calls carrying the identical set
        produce byte-identical request bodies (useful for anything diffing
        requests, e.g. a test or a proxy log), same rationale as every other
        JSON-body method on this client that accepts an unordered input.

        EMPTY is a legitimate policy shape to cabal -- an empty set means
        "wide open" there, by its own design (colleagues switchboard plan,
        D-017) -- so this method always pushes exactly what it's given,
        never treating `not pairs` as "nothing to do" and skipping the
        call. But because an accidental empty push would silently OPEN the
        switchboard rather than closing it, a push that computes to empty
        right after THIS SAME CLIENT last pushed something non-empty is
        logged loudly (`log.warning`, naming the shrink) before the request
        even goes out -- still pushed regardless (policy is policy, and a
        real revoke-everything moment must not get silently swallowed
        here) -- while every push, empty or not, logs its resulting count
        at `log.info` so a room's ordinary logs show the switchboard policy
        actually moving.

        Raises `CabalError` on non-2xx, same logging convention as every
        other method here: the response body is logged server-side
        (truncated), never echoed into the raised message. Either that or a
        raw transport `httpx.HTTPError` marks `dispatch_pairs_dirty` True
        before propagating (final-review fix round, HIGH-2b -- see that
        flag's own `__init__` comment); a push that goes on to succeed,
        whether this is its first attempt or a retry after one of those
        failures, always clears it back to False.
        """
        sorted_pairs = sorted(pairs)
        if not sorted_pairs and self._last_dispatch_pairs_nonempty:
            log.warning(
                "cabal put_dispatch_pairs: pushing an EMPTY pair set right "
                "after this client's last push was non-empty -- cabal "
                "treats an empty set as WIDE OPEN by design, so this is a "
                "real access-control shrink, not a skipped no-op"
            )
        try:
            resp = self._client.put(
                f"{self._base_url}/admin/dispatch-pairs",
                json={"pairs": [list(pair) for pair in sorted_pairs]},
                headers=self._headers(),
            )
        except httpx.HTTPError:
            self._dispatch_pairs_dirty = True
            raise
        if not resp.is_success:
            log.warning(
                "cabal put_dispatch_pairs failed (%s): %s",
                resp.status_code, resp.text[:200],
            )
            self._dispatch_pairs_dirty = True
            raise CabalError(f"put_dispatch_pairs failed ({resp.status_code})")
        count = resp.json()["count"]
        log.info("cabal put_dispatch_pairs: pushed %d pair(s)", count)
        self._last_dispatch_pairs_nonempty = bool(sorted_pairs)
        self._dispatch_pairs_dirty = False
        return count

    def list_conversations(
        self,
        *,
        limit: int | None = None,
        target: str | None = None,
        caller: str | None = None,
    ) -> list[dict]:
        """List conversations recorded on the switchboard's admin surface
        (`GET /admin/conversations`) — the room's watching window (Task
        4/B4), one row per relayed a2a task: `task_id`, `target_agent`,
        `target_generation`, `caller_agent`, `state`, `cabal_escalated_
        from`, `created_at`, `updated_at`, `result_snippet` (<=120 chars).

        Each param is sent as a query param only when given. BEWARE the
        omitted-`limit` trap: the switchboard's own default is 50 rows,
        room-wide, most-recent first (and it clamps any explicit limit to
        200) — "no limit" never meant unbounded, whatever this docstring
        used to claim. `target` scopes to conversations an agent ANSWERED;
        `caller` (matched verbatim against `caller_agent`, e.g.
        `agent:ward`) to the ones it STARTED — the two halves of
        participant scoping, server-side, so a consumer never has to fish
        one agent's history out of the room-wide window.

        Returns the parsed `"conversations"` list verbatim. Raises
        `CabalError` on non-2xx, same logging convention as every other
        method here (response body logged server-side, truncated, never
        echoed into the raised message).
        """
        params: dict[str, int | str] = {}
        if limit is not None:
            params["limit"] = limit
        if target is not None:
            params["target"] = target
        if caller is not None:
            params["caller"] = caller
        resp = self._client.get(
            f"{self._base_url}/admin/conversations",
            params=params,
            headers=self._headers(),
        )
        if not resp.is_success:
            log.warning(
                "cabal list_conversations failed (%s): %s",
                resp.status_code, resp.text[:200],
            )
            raise CabalError(f"list_conversations failed ({resp.status_code})")
        return resp.json()["conversations"]

    def get_conversation(self, task_id: str) -> dict:
        """One conversation's full detail plus its event log (`GET
        /admin/conversations/{task_id}`) — the watching window's expanded
        view (Task 4/B4). Returns the parsed body verbatim: a
        `"conversation"` object (the full, untruncated result) and an
        `"events"` list (each `{id, event_type, payload, created_at}`).

        Raises `CabalError` on non-2xx, same logging convention as every
        other method here.
        """
        resp = self._client.get(
            f"{self._base_url}/admin/conversations/{task_id}",
            headers=self._headers(),
        )
        if not resp.is_success:
            log.warning(
                "cabal get_conversation(%s) failed (%s): %s",
                task_id, resp.status_code, resp.text[:200],
            )
            raise CabalError(f"get_conversation({task_id!r}) failed ({resp.status_code})")
        return resp.json()

    # A long-lived connection needs SOME bound, or a silent upstream (no
    # bytes, connection never drops) hangs a caller forever with no way
    # out; 300s comfortably outlasts the switchboard's own keepalive
    # comments (it sends at least one `: ...\n\n` well inside that window)
    # while still being finite. connect/write/pool stay at the class's own
    # everyday 10s — only the read side needs the SSE-shaped headroom.
    _STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0)

    async def stream_events(self) -> AsyncIterator[bytes]:
        """Open a streaming GET to the switchboard's live event feed
        (`GET /admin/events/stream`) and yield raw byte chunks exactly as
        received — a byte-for-byte passthrough, no per-frame parsing or
        re-encoding, since the only consumer (`GET /api/colleagues/stream`,
        kobo_api.main) just relays the switchboard's own SSE bytes straight
        to the browser's EventSource unchanged.

        Fix round 1 (fixing an Important review finding on Task 4/B4): this
        is an ASYNC generator over `self._async_client` (a real
        `httpx.AsyncClient`, never the sync `self._client` every other
        method here uses), not a sync one. The original sync version was
        consumed by Starlette via `iterate_in_threadpool` (since the route
        handed it straight to `StreamingResponse`), and a blocking
        synchronous read can't be interrupted by asyncio task
        cancellation — a client disconnecting mid-stream left the
        switchboard connection AND a worker thread hung until upstream
        happened to send more bytes. An async generator, awaited directly
        on the event loop (the route below now consumes it with no
        threadpool involved), lets cancellation propagate the moment the
        client goes away.

        `_STREAM_TIMEOUT` bounds the read side at 300s (generous — SSE
        keepalives arrive well inside that window — but finite, unlike the
        old `timeout=None`) so a silently-stalled upstream still eventually
        frees this call rather than hanging forever; connect/write/pool
        stay at this class's everyday 10s.

        Ends the generator (no more chunks, no exception) rather than
        raising on either kind of failure a caller must tolerate silently:
        a non-2xx response (bad/expired operator token, switchboard
        trouble — never forwarded, since blindly relaying an error page's
        body as if it were legitimate event-stream content would be
        actively misleading to the browser's EventSource) or a transport
        failure (connection refused, dropped mid-stream, or a read timeout
        past the bound above). The switchboard's own stream never replays
        missed events anyway, so a caller reconnecting from scratch after
        any kind of drop loses nothing it wouldn't already accept losing.
        The route (`/api/colleagues/stream`) counts on this: it never
        wraps this generator in a try/except of its own, trusting every
        failure mode to end iteration cleanly instead of surfacing as a
        mid-response exception.
        """
        try:
            async with self._async_client.stream(
                "GET",
                f"{self._base_url}/admin/events/stream",
                headers=self._headers(),
                timeout=self._STREAM_TIMEOUT,
            ) as resp:
                if not resp.is_success:
                    log.warning("cabal stream_events failed (%s)", resp.status_code)
                    return
                async for chunk in resp.aiter_bytes():
                    yield chunk
        except httpx.HTTPError:
            return


# --- Colleagues conversation scoping on the SSE stream (access-tab refinement
# wave, conversation scoping) -------------------------------------------------
#
# `GET /api/colleagues/stream` (kobo_api.main) wraps `CabalClient.stream_
# events()`'s raw byte passthrough with this filter whenever the caller
# supplies `?agent=<name>` -- forwarding only the frames belonging to a
# conversation that agent participates in. The switchboard's own event wire
# shape: every frame's `data:` line is JSON `{"event_type": ..., "task_id":
# ..., "payload": {...}}`. Only `dispatch_start`'s own payload ever embeds
# who's talking to whom (`target`/`caller`) -- the same two facts `CabalClient.
# list_conversations`'s row shape carries as `target_agent`/`caller_agent`
# (see that method's own docstring); every later event for that task_id
# (`dispatch_success`/`dispatch_failure`) carries only state/error, never the
# participants again.


def _sse_frame_json(frame: bytes) -> tuple[str | None, dict | None]:
    """Parse one SSE frame's body (the bytes between two `\\n\\n`
    terminators, terminator not included) into `(event_name, payload)`.
    `event_name` comes from the frame's `event:` line (`None` when absent).
    `payload` is the JSON-decoded `data:` line joined across every `data:`
    line the frame carries (`None` when there's no `data:` line at all --
    a bare keepalive comment -- or when what's there isn't valid JSON, or
    isn't a JSON object once parsed): callers must treat `payload is None`
    as "nothing here to filter on," never as an empty dict.
    """
    event_name: str | None = None
    data_lines: list[str] = []
    for raw_line in frame.split(b"\n"):
        line = raw_line.decode("utf-8", errors="replace")
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
    if not data_lines:
        return event_name, None
    try:
        payload = json.loads("\n".join(data_lines))
    except ValueError:
        return event_name, None
    return event_name, payload if isinstance(payload, dict) else None


def _event_matches_agent(
    payload: dict, agent: str, task_participants: dict[str, bool]
) -> bool:
    """Whether one decoded switchboard event names `agent` as a
    participant, consulting (and updating) `task_participants` -- the
    per-connection `task_id -> matches` memory `filter_cabal_event_stream`
    keeps for the life of one SSE connection.

    Only `dispatch_start`'s own payload carries the conversation's
    participants directly (`target`/`caller`); every later event for the
    same `task_id` carries only state/error, so this looks up what an
    earlier `dispatch_start` in THIS SAME connection already recorded for
    that `task_id`. A `task_id` this connection never saw a `dispatch_
    start` for (the conversation began before this connection opened --
    the switchboard never replays history over this feed, `CabalClient.
    stream_events`'s own docstring) has no participant memory to consult
    and is dropped -- fail-closed, the same posture `kobo_api.groups`'
    `None`-means-grant-nothing contract takes on an unanswerable question:
    an event this filter cannot confirm belongs to `agent` is treated as
    if it doesn't.

    `agent` matches either side: as the TARGET (a bare name, `payload.
    get("target")`) or as the CALLER (`payload.get("caller")`, which is
    only ever `agent:<name>` for a genuine colleague-to-colleague call --
    `CabalClient.mint_colleague`'s own `sub` shape -- so a human/service
    caller can never falsely match here).
    """
    task_id = payload.get("task_id")
    inner = payload.get("payload")
    target = inner.get("target") if isinstance(inner, dict) else None
    caller = inner.get("caller") if isinstance(inner, dict) else None
    if target is not None or caller is not None:
        matches = target == agent or caller == f"agent:{agent}"
        if isinstance(task_id, str):
            task_participants[task_id] = matches
        return matches
    if isinstance(task_id, str) and task_id in task_participants:
        return task_participants[task_id]
    return False


def _event_is_thread(payload: dict, task_threads: dict[str, bool]) -> bool:
    """Whether one decoded switchboard event belongs to a thread turn
    (Critical 3 fix, thread exclusion on the SSE stream) -- a thread turn's
    relay stamps `metadata: {"thread": <id>}` on the envelope
    (`kobo_api.main.post_thread_turn`), and `dispatch_start`'s own payload
    echoes that submitted envelope, `metadata` included, same as it already
    does for `target`/`caller` (`_event_matches_agent` above). Same
    per-connection memory pattern: only `dispatch_start` ever carries
    `metadata`, so a later event for the same `task_id` (`dispatch_success`/
    `dispatch_failure`) consults what an earlier `dispatch_start` in THIS
    SAME connection already recorded for it.

    A `task_id` this connection never saw a `dispatch_start` for reads as
    "not a thread" -- the switchboard's own events are ordered start-then-
    terminal per task, so a genuinely thread-tagged task_id always has its
    dispatch_start seen first in any connection that observes it at all;
    there is no window where a terminal event for a real thread arrives
    with no prior memory of it.
    """
    task_id = payload.get("task_id")
    inner = payload.get("payload")
    metadata = inner.get("metadata") if isinstance(inner, dict) else None
    if isinstance(metadata, dict):
        is_thread = "thread" in metadata
        if isinstance(task_id, str):
            task_threads[task_id] = is_thread
        return is_thread
    if isinstance(task_id, str) and task_id in task_threads:
        return task_threads[task_id]
    return False


async def filter_cabal_event_stream(
    chunks: AsyncIterator[bytes], agent: str | None = None
) -> AsyncIterator[bytes]:
    """Wrap `CabalClient.stream_events()`'s raw byte passthrough with the
    colleagues conversation-scoping filter (`GET /api/colleagues/stream`,
    kobo_api.main): forwards only frames belonging to a conversation
    `agent` participates in (`_event_matches_agent` above) when `agent` is
    given, plus every frame this filter can't attribute to any one
    conversation at all -- a bare keepalive comment, or the connection's
    own "hello" banner -- since those aren't conversation content at all,
    so scoping to one agent has no bearing on them and dropping them would
    only break the client's own connection bookkeeping for no privacy
    benefit.

    Critical 3 fix: thread exclusion (`_event_is_thread` above) now runs
    UNCONDITIONALLY, before any agent scoping and regardless of whether
    `agent` is given at all -- a thread is a private, initiator-only
    conversation (operator ruling) that must never reach this stream, the
    same posture `_flatten_conversation_entries` already takes for the
    colleague feed. Before this fix, `agent=None` meant the raw upstream
    bytes were returned completely unwrapped by the route
    (`stream_colleague_events`), so a thread turn's dispatch events -- full
    transcript included, since a thread turn resends the whole conversation
    on every relay -- reached any signed-in caller watching the room-wide
    stream. The route now always wraps with this filter, `agent=None`
    simply skipping the agent-match step below.

    Buffers raw bytes and re-splits on the SSE frame terminator (a blank
    line, `\\n\\n`) rather than trusting chunk boundaries to line up with
    frame boundaries -- `stream_events` gives no such guarantee, same "don't
    trust the transport's own chunking" discipline as any other byte-stream
    parser. A trailing partial frame still in the buffer when the upstream
    ends (the connection dropped mid-frame) is simply discarded, matching
    `stream_events`'s own "ends the generator, never raises" contract: there
    is no complete frame left to decide on, so nothing is lost that a fresh
    reconnect wouldn't already have to re-fetch anyway.
    """
    buffer = b""
    task_participants: dict[str, bool] = {}
    task_threads: dict[str, bool] = {}
    async for chunk in chunks:
        buffer += chunk
        while b"\n\n" in buffer:
            frame, buffer = buffer.split(b"\n\n", 1)
            event_name, payload = _sse_frame_json(frame)
            if payload is None or event_name == "hello":
                forward = True
            elif _event_is_thread(payload, task_threads):
                forward = False
            elif agent is not None:
                forward = _event_matches_agent(payload, agent, task_participants)
            else:
                forward = True
            if forward:
                yield frame + b"\n\n"


def _relay_reply_parts_sources(task: dict) -> list[list]:
    """Every parts list a relayed switchboard task might carry the reply
    text in, most-authoritative first: `status.message.parts` (the exact
    path the direct path's `_post_task`, above, reads), then the last turn
    of `history`, then the last entry of `artifacts` — a richer
    completed-task shape a real a2a-compliant agent behind the switchboard
    can return that this room's own agents (which only ever exercise the
    first path, via `KubectlAgentClient`) never have.
    """
    sources: list[list] = []

    status = task.get("status")
    if isinstance(status, dict):
        message = status.get("message")
        if isinstance(message, dict) and isinstance(message.get("parts"), list):
            sources.append(message["parts"])

    history = task.get("history")
    if isinstance(history, list) and history:
        last_turn = history[-1]
        if isinstance(last_turn, dict) and isinstance(last_turn.get("parts"), list):
            sources.append(last_turn["parts"])

    artifacts = task.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        last_artifact = artifacts[-1]
        if isinstance(last_artifact, dict) and isinstance(last_artifact.get("parts"), list):
            sources.append(last_artifact["parts"])

    return sources


def _extract_relay_reply(response: dict) -> str:
    """Pull the reply text out of a relayed switchboard response.

    Shared by every caller that relays an a2a task through `CabalClient.
    relay` and needs to read the completed task back out (`kobo_api.main`'s
    chat route, `kobo_api.steps`' `HelloStep` first-contact path,
    colleagues switchboard plan Task 4b) — kept here, next to `CabalClient`
    and `_build_task_envelope`, rather than duplicated in each caller's own
    module, so the two never drift apart. Neither caller module imports the
    other; both already import this one.

    The response JSON root may carry the completed task bare, or nested
    under a `"task"` key (a request shape some caller echoes back) or a
    `"result"` key (the JSON-RPC-style envelope some a2a servers use) — both
    wrappings are unwrapped first. For each parts list `_relay_reply_parts_sources`
    returns (most-authoritative first), the extraction joins ALL text parts
    in order (`_join_text_parts`, the family contract); a source whose parts
    carry no text is skipped and extraction moves to the next source. This
    matches the direct path's own semantics, which joins the same way on the
    authoritative source.

    Raises `CabalError` when no text part turns up in any source — callers
    map that to the same failure handling a genuine relay failure gets,
    since either way the switchboard didn't hand back something usable.
    """
    task = response
    if isinstance(response, dict):
        if isinstance(response.get("task"), dict):
            task = response["task"]
        elif isinstance(response.get("result"), dict):
            task = response["result"]

    if not isinstance(task, dict):
        raise CabalError("malformed switchboard reply: not a task object")

    for parts in _relay_reply_parts_sources(task):
        text = _join_text_parts(parts)
        if text:
            return text

    raise CabalError("malformed switchboard reply: no text part found")


# --- Wiring -----------------------------------------------------------------------


def installation_token_provider(config: Config) -> InstallationTokenProvider | None:
    """The GitHub App installation-token provider for `config`, or None when
    the App is not fully configured (dev = operator's gh login). Callers take
    `.token` for `GhCLI.token_provider` and `.can_write_contents` for its
    `installation_writes`, from the one object, so both read one mint.

    Extracted out of `Clients.real` so `build_default_app` (kobo_api.main)
    can wire the same App-authed provider into the separate GhCLI it builds
    for the boot-time org-config clone — a PRIVATE org-config repo needs App
    auth there too, not just in the Clients built afterward.
    """
    # Local import: kobo_api.github_app imports ClientError from this
    # module, so a module-level import here would be circular. Deferred to
    # call time, by which point both modules are fully loaded.
    from kobo_api.github_app import InstallationTokenProvider

    if (
        config.github_app_id
        and config.github_app_installation_id
        and config.github_app_private_key_path
    ):
        return InstallationTokenProvider(
            config.github_app_id,
            config.github_app_installation_id,
            config.github_app_private_key_path,
        )
    return None


def _universe_from_groups(groups: list[GroupChoice]) -> dict[str, list[str]]:
    """`config.groups` (a LIST, so nothing stops two entries sharing a
    `name` -- a config typo, or two org-config layers each contributing
    their own entry for the same room group) collapsed into the
    `OktaGroupResolver` constructor's config-name -> [okta-names] shape.

    Merges (unions, deduped, order-preserving) rather than the naive
    `{group.name: group.okta_groups for group in groups}` dict
    comprehension this replaced, whose last-write-wins behavior silently
    dropped every earlier entry's okta names the moment two `GroupChoice`s
    shared a `name` -- a flagged Low that's actually reachable: this
    directory's own regional split means an operator adding "the india
    group too" for an existing team is one YAML entry away from being that
    second, colliding entry. `OktaGroupResolver`'s own construction-time
    `_invert_universe` still separately rejects a single Okta name claimed
    by two DIFFERENT config names -- that's a distinct failure this merge
    step does not and should not try to paper over.
    """
    merged: dict[str, list[str]] = {}
    for group in groups:
        okta_names = merged.setdefault(group.name, [])
        for okta_name in group.okta_groups:
            if okta_name not in okta_names:
                okta_names.append(okta_name)
    return merged


@dataclass
class Clients:
    github: GitHubClient
    gojira: GojiraClient
    cloud: CloudClient
    agent: AgentClient
    kubectl: KubectlClient
    # None when Slack connect isn't configured (neither token store — see
    # `slack_configured`) — routes must 503 with a clear "Slack is not
    # configured" error in that case, keeping local setups without Slack
    # tokens working untouched.
    slack: SlackClient | None = None
    # Unlike slack, ImageClient is always constructible (the vertex project
    # fields it needs already exist in Config) — Clients.real() below never
    # gates this behind an "is this configured" check the way it does slack;
    # it always assigns a real VertexImageClient. The dataclass field itself
    # still defaults to None purely so every pre-existing direct
    # `Clients(...)` construction elsewhere in the test suite (predating the
    # avatar feature) keeps working without needing to pass this field.
    image: ImageClient | None = None
    # Same move as image: gcp_project already exists in Config for other
    # clients, so storage is always constructible — Clients.real() below
    # never gates it behind an "is this configured" check. Defaults to None
    # purely so pre-existing direct `Clients(...)` constructions in the test
    # suite keep working without passing this field.
    storage: StorageClient | None = None
    # Same "always constructible" move as image/storage above (vertex_project
    # already exists in Config; name_location/name_model are the only new
    # fields, mirroring avatar_location/avatar_model) — Clients.real() below
    # never gates this behind an "is this configured" check either. Defaults
    # to None purely so pre-existing direct `Clients(...)` constructions in
    # the test suite keep working without passing this field; the
    # name-suggestions route (kobo_api.main) treats a None the same as a
    # generator ClientError — fall through to the curated fallback pool.
    name: NameClient | None = None
    # None whenever `cabal_enabled(config)` is false (same "absent = feature
    # off" convention as slack above) — every switchboard-facing caller
    # (DirectoryStep, the destroy stage, the metadata tails, the sync route)
    # checks `cabal_enabled` itself before ever touching this field, so a
    # None here is never dereferenced on an unwired room.
    cabal: CabalClient | None = None
    # None whenever the directory integration isn't wired (no OKTA_API_TOKEN
    # env var, or config.groups/config.okta_domain left unset) — same
    # "absent = feature off" convention as cabal above. Every group-aware
    # caller (access-control arc, Task B2+) must treat a None here as fail
    # closed on its own terms too: no resolver configured is not license to
    # grant group-derived visibility by default, only owner/operator rights
    # ever survive that.
    groups: GroupResolver | None = None
    # None whenever the directory integration isn't wired -- same "absent =
    # feature off" convention as groups above, but deliberately independent
    # of `config.groups`: people search (access-tab refinement wave) is a
    # directory-wide lookup, not an ACL feature, so it only ever needs the
    # env token and the domain, never a configured groups universe. The
    # people-search route (kobo_api.main) treats a None here the same as a
    # live search failure -- fail-soft, `{"people": [], "unavailable":
    # true}`, never a 5xx.
    people_search: OktaPeopleSearch | None = None
    # None whenever the secrets platform's machine lane isn't configured
    # (`Config.ladon_enabled` false -- the same "absent = feature off"
    # convention as cabal above). Set, it REPLACES the direct
    # `kubectl.apply_secret` write at the room's two per-agent secret
    # deliveries (the colleague pass and the Slack tokens); there is
    # deliberately no fallback between the two, since an agent whose Secret
    # has two writers has two sources of truth. See kobo_api.ladon.
    ladon: LadonClient | None = None

    @classmethod
    def real(cls, config: Config) -> Clients:
        """Wire the real, subprocess/network-backed implementations."""
        # Local import for the same circularity reason as
        # `installation_token_provider`'s: kobo_api.ladon imports
        # ClientError from this module.
        from kobo_api.ladon import HttpxLadonClient

        runner = CommandRunner()
        provider = installation_token_provider(config)
        token_provider = provider.token if provider else None
        installation_writes = provider.can_write_contents if provider else None
        # One shared CachedGcloudToken across every client that defaults its
        # own token_provider to `_gcloud_print_access_token` (image, name,
        # storage) — a token minted for one is reused by the others until it
        # goes stale, instead of each shelling out to `gcloud auth
        # print-access-token` (~1-2s live) on every single call.
        gcloud_token = CachedGcloudToken()
        # Built before the `cls(...)` call rather than inline: the Slack
        # token store may itself be kubectl-backed, and it reads the pair
        # through this same client (one binary, one runner) rather than a
        # second one built just for that.
        kubectl = KubectlCLI(runner=runner)
        slack_store = slack_token_store(config, kubectl)
        return cls(
            github=GhCLI(
                runner=runner,
                token_provider=token_provider,
                installation_writes=installation_writes,
            ),
            gojira=HttpxGojiraClient(config.gojira_url, token=config.gojira_token),
            cloud=GcloudClient(config.gcp_project, runner=runner),
            agent=KubectlAgentClient(),
            kubectl=kubectl,
            image=VertexImageClient(
                config.vertex_project, config.avatar_location, config.avatar_model,
                token_provider=gcloud_token,
            ),
            name=VertexNameClient(
                config.vertex_project, config.name_location, config.name_model,
                token_provider=gcloud_token,
            ),
            storage=HttpxStorageClient(config.gcp_project, token_provider=gcloud_token),
            slack=HttpxSlackClient(slack_store) if slack_store is not None else None,
            # Same "is this configured" gate as slack above, but via the
            # shared `cabal_enabled` truthiness check (kobo_api.clients)
            # rather than a single field, since being wired needs all three
            # of cabal_url/agent_endpoint_template/the operator token env
            # var — cabal_operator_token() is only ever called here (never
            # cached, so a token delivered after import is still picked up)
            # and inside CabalClient itself never anywhere it could leak.
            cabal=CabalClient(config.cabal_url, cabal_operator_token())
            if cabal_enabled(config)
            else None,
            # Same "is this configured" gate shape as cabal above: all three
            # of OKTA_API_TOKEN (env, read here and only here — never
            # cached at module scope, never logged), config.groups, and
            # config.okta_domain must be present, or group-derived
            # visibility simply isn't wired (routes fall back to
            # owner/operator rights only, per the fail-closed contract in
            # kobo_api.groups).
            groups=OktaGroupResolver(
                config.okta_domain,
                os.environ.get("OKTA_API_TOKEN", ""),
                _universe_from_groups(config.groups),
            )
            if os.environ.get("OKTA_API_TOKEN") and config.groups and config.okta_domain
            else None,
            # Same env-token-and-domain gate as groups above, but WITHOUT
            # the `config.groups` requirement -- see the field's own
            # docstring for why (people search is directory-wide, not an
            # ACL feature).
            people_search=OktaPeopleSearch(
                config.okta_domain, os.environ.get("OKTA_API_TOKEN", "")
            )
            if os.environ.get("OKTA_API_TOKEN") and config.okta_domain
            else None,
            # All three coordinates or nothing (`Config.ladon_enabled`),
            # same gate shape as cabal above. The client's credential is
            # this workload's own identity, minted per call from the
            # metadata server — nothing configured, nothing stored.
            ladon=HttpxLadonClient(
                config.ladon_url, config.ladon_audience, config.ladon_namespace
            )
            if config.ladon_enabled
            else None,
        )
