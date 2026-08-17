"""Tooling module: the staged door for an agent's LAW-to-be (2026-08-13
addendum). Files land at `bundle/tools/<filename>.{py,go}` in the agent's own
repo — the same bundle of record `kobo_api.knowledge`/`kobo_api.intake`/
`kobo_api.skills` operate on. Unlike knowledge (freely-edited seed memory)
and skills (informs the model), a tooling file is meant to eventually BIND
the runtime: an orchestrator or guardrail the enforcement contract (a later,
separate design) will always execute around a reply.

That enforcement contract shipped on the runtime side as of the 2026-08
addendum (Task 2's card stamp: a running pod's `/.well-known/agent-card.json`
now carries an `enforcement` block once it executes `bundle/tools/*` hooks
around a reply). Before that runtime rolls out to a given agent — or when
kobo simply can't reach the pod to ask — a stored tooling file is still
inert, the runtime's bundle loader ignoring unknown top-level dirs the same
as always. This module's job is honest, staged plumbing either way: validate
a file mechanically (the law it WILL be held to, checked now so nothing
malformed ever lands), store it, report whether it has been delivered to a
running pod, and — Task 4 — probe that same pod's card for whether
enforcement is actually live there. `list_tooling`'s "delivered" derives
from the exact same ancestor-based reasoning `kobo_api.intake.list_intake`
uses for its own "ingested" status (see that function's docstring for the
full algorithm) — pure boot-ordering inference, no kobo-side state to keep
in sync. The ~15-line derivation loop is duplicated here rather than shared
with `list_intake`: the two operate on different sub-trees
(`bundle/tools/` vs `bundle/intake/`) and mean subtly different things
("delivered to the running pod" vs "the seeder has ingested it"), so forcing
a shared helper across them would blur that distinction for a small amount
of saved code. The card probe (`_probe_enforcement`) keeps the same
never-guess discipline: any fetch or parse failure reads as "not attested",
never as a guess dressed up as a real answer.

Two doors write into the same `bundle/tools/` tree, sharing one validator
(`_validate_filename`/`_validate_content`): `upload_tooling_file` (a single
file, from a form) and `import_tooling` (every `.py`/`.go` file in a folder
of some OTHER repo, cloned via the same GitHub App identity that clones the
agent's own repo). Import validates each candidate independently and commits
only the ones that pass in one push — one bad file in a folder of ten must
never sink the other nine. `_validate_content` is deliberately the ONE place
that runs the mechanical checks (extension is checked earlier, by
`_validate_filename`, since it's part of the filename shape, not the
content): the enforcement contract's own import allowlist and entrypoint
check (2026-08-03 constraints) join this same function, reusing the exact
same `ast.parse` tree the syntax check already produced, so both doors — and
any later per-agent write path — pick them up for free.

Every mutating operation here clones fresh into a `tempfile.
TemporaryDirectory` and discards it before returning, same discipline as
`kobo_api.intake`/`kobo_api.knowledge` — no local working copy kept around
between calls. Uploads and imports both re-pin the runtime image in the same
commit as the file write, via the same `runtime_image` parameter and the
same `kobo_api.composer.update_dockerfile_runtime_image` helper
`kobo_api.intake.upload_intake_file` uses — the false-delivered-window fix:
an agent composed before a runtime bump ships every image FROM the old
runtime, and without a re-pin, this door's own delivery would boot an image
whose runtime never runs the enforcement contract once it exists, yet
`list_tooling`'s ancestor derivation would still call the file "delivered"
the instant the live commit descends from this push. See
`kobo_api.intake`'s module docstring for the full incident this pattern
closes.
"""

from __future__ import annotations

import ast
import re
import sys
import tempfile
from pathlib import Path

from kobo_api.clients import (
    AgentClient,
    ClientError,
    GitHubClient,
    KubectlClient,
    image_tag,
)
from kobo_api.composer import update_dockerfile_runtime_image

# 1-64 chars: a lowercase letter, then lowercase/digit/underscore/hyphen, then
# ".py" or ".go" — same no-path-separator, no-leading-dot/digit/hyphen/
# underscore shape as kobo_api.intake.INTAKE_FILENAME_RE, just admitting
# underscores in the body (code names use them) and a longer cap (a real
# orchestrator file's name can run longer than a knowledge note's).
TOOLING_FILENAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}\.(py|go)$")

TOOLING_MAX_BYTES = 524288

# owner/repo shape for import_tooling's source_repo: two path segments, each
# the characters GitHub itself allows in an owner or repo name (letters,
# digits, dot, underscore, hyphen) — never more or fewer than one slash, so a
# bare name (no owner) or a deeper path can't slip through as a "repo".
SOURCE_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

# Extensions import_tooling's non-recursive folder walk considers at all —
# .go syntax isn't checked (see _validate_content), but a .go file is still a
# candidate the walk picks up and the filename/size/UTF-8/non-empty checks
# still run against.
_TOOLING_EXTENSIONS = (".py", ".go")


class ToolingError(Exception):
    """A tooling filename/source-repo/source-path failed validation, a
    file's content was empty, too large, not valid UTF-8, or (for a .py
    file) failed to `ast.parse`, imported something beyond the standard
    library, used a relative import, defined an async `pre_task`/
    `post_draft` hook, defined neither hook at all, or a delete named a
    file that isn't there.
    """


def _validate_filename(filename: str) -> None:
    # fullmatch, not match: see kobo_api.intake._validate_filename's
    # docstring for why `.match()` would let a trailing-newline filename
    # slip past (house lesson, repeated here verbatim).
    if not TOOLING_FILENAME_RE.fullmatch(filename):
        raise ToolingError(f"invalid tooling filename: {filename!r}")


# The two hook names the (not-yet-shipped) enforcement contract will call by
# name on a tooling file — see `_check_entrypoint`.
_ENTRYPOINT_NAMES = ("pre_task", "post_draft")


def _check_import_allowlist(tree: ast.AST) -> None:
    """Walk the WHOLE tree — every `import X` / `from X import ...`
    anywhere in the file, not just top-level, so a stdlib-evading import
    tucked inside a function body doesn't slip past. A relative import
    (`from . import x`, `from .pkg import y` — `node.level > 0`) is rejected
    outright: the flat `bundle/tools/` directory has no package structure
    for a relative import to resolve against, so there is no line number of
    code where one could ever work. Anything else is checked against
    `sys.stdlib_module_names` by its top-level module name
    (`name.split(".")[0]`) — the tooling file's only allowed dependency
    surface until the enforcement contract defines a real one.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level = alias.name.split(".")[0]
                if top_level not in sys.stdlib_module_names:
                    raise ToolingError(
                        "tooling file imports beyond the standard library "
                        f"on line {node.lineno}: {top_level}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                raise ToolingError(
                    f"tooling file uses a relative import on line {node.lineno}"
                )
            top_level = (node.module or "").split(".")[0]
            if top_level not in sys.stdlib_module_names:
                raise ToolingError(
                    "tooling file imports beyond the standard library "
                    f"on line {node.lineno}: {top_level}"
                )


def _check_entrypoint(tree: ast.Module) -> None:
    """At least one top-level `def pre_task(...)` or `def post_draft(...)`
    is required — only `tree.body` (the module's own top-level statements)
    counts, so a `def pre_task` nested inside another function does NOT
    satisfy this: it isn't reachable the way the runtime would call it,
    which is exactly the mechanical honesty this module's docstring
    describes. A top-level hook defined `async def` is rejected outright
    with its own message, distinct from "neither hook defined" — the
    (also not-yet-shipped) call site is synchronous.
    """
    found = False
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in _ENTRYPOINT_NAMES:
            raise ToolingError(
                "tooling file hooks must be regular functions, not async"
            )
        if isinstance(node, ast.FunctionDef) and node.name in _ENTRYPOINT_NAMES:
            found = True
    if not found:
        raise ToolingError("tooling file defines neither pre_task nor post_draft")


def _validate_content(filename: str, raw: bytes) -> str:
    """The single mechanical validator (module docstring): size cap, UTF-8
    decodability, non-empty, and — for a `.py` file — `ast.parse`, an
    import allowlist (standard library only, no relative imports), and an
    entrypoint check (a top-level `pre_task` or `post_draft`). Returns
    the decoded text on success.

    Takes raw bytes rather than an already-decoded str so `import_tooling`
    can run this against bytes read straight off disk (a candidate file in
    someone else's repo is not guaranteed to be valid UTF-8 at all);
    `upload_tooling_file`'s own str content is just `.encode("utf-8")`'d
    first, since a str reaching this module from a JSON request body already
    decoded cleanly by the time FastAPI handed it over — that first encode
    can never itself raise.

    Size is checked against the RAW byte length (not the decoded string's
    character count) — the actual number of bytes this pushes into the
    repo, matching `kobo_api.intake`'s own convention
    (`len(content.encode("utf-8"))`).
    """
    size = len(raw)
    if size > TOOLING_MAX_BYTES:
        raise ToolingError(
            f"tooling file content is {size} bytes, exceeds the "
            f"{TOOLING_MAX_BYTES}-byte limit"
        )

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolingError("tooling file content is not valid UTF-8 text") from exc

    if not content:
        raise ToolingError("tooling file content must not be empty")

    if filename.endswith(".py"):
        # .go gets no syntax check at all (module docstring, and the
        # addendum: no Go toolchain in the room) — validation of a .go
        # file's actual syntax is deferred to the enforcement contract. The
        # SAME parsed tree is reused below for the import allowlist and
        # entrypoint checks — one `ast.parse` call, three checks off it.
        try:
            tree = ast.parse(content)
        except SyntaxError as exc:
            line = exc.lineno if exc.lineno is not None else "unknown"
            raise ToolingError(
                f"tooling file has a syntax error on line {line}: {exc.msg}"
            ) from exc

        _check_import_allowlist(tree)
        _check_entrypoint(tree)

    return content


def _validate_source_repo(source_repo: str) -> None:
    if not SOURCE_REPO_RE.fullmatch(source_repo):
        raise ToolingError(f"invalid source repo: {source_repo!r}")


def _validate_source_path(source_path: str) -> None:
    # No leading "/" (an absolute path escapes the clone entirely) and no
    # ".." segment anywhere (a relative escape out of the clone) — the same
    # no-traversal shape, expressed directly rather than via a filename
    # regex, since a folder path may nest arbitrarily deep, unlike a bare
    # filename.
    if source_path.startswith("/") or any(
        part == ".." for part in source_path.split("/")
    ):
        raise ToolingError(f"invalid source path: {source_path!r}")


def upload_tooling_file(
    github: GitHubClient,
    repo: str,
    filename: str,
    content: str,
    *,
    runtime_image: str | None = None,
) -> str:
    """Write (or overwrite) `bundle/tools/<filename>` and push it.

    Validates before ever cloning: filename shape/extension, then content
    (size/UTF-8/non-empty/ast, via the shared `_validate_content`). Returns
    the pushed commit's local sha (`github.local_head`), same
    no-op-but-still-returns-head semantics as `kobo_api.intake.
    upload_intake_file`: byte-identical content leaves `commit_push`
    reporting False (nothing to commit), but this still returns
    `local_head` — the truthful answer, since nothing changed and whatever
    was live before still is.

    `runtime_image`, when given (the route always passes the org's current
    `Config.runtime_image`), re-pins the clone's Dockerfile `FROM` line to
    it in this SAME commit — see the module docstring for why. `None` (the
    default) skips the edit entirely.
    """
    _validate_filename(filename)
    _validate_content(filename, content.encode("utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        tools_dir = workdir / "bundle" / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        (tools_dir / filename).write_text(content, encoding="utf-8")
        if runtime_image is not None:
            update_dockerfile_runtime_image(workdir / "Dockerfile", runtime_image)
        github.commit_push(workdir, f"kobo: add tools/{filename}")
        return github.local_head(workdir)


def delete_tooling_file(github: GitHubClient, repo: str, filename: str) -> str:
    """Remove `bundle/tools/<filename>` and push the removal.

    These are files, not memories (module docstring) — there is no "the
    runtime already saw it" concern deleting has to be honest about the way
    `kobo_api.intake.delete_intake_file` is; removing the file removes it,
    full stop. Raises `ToolingError` if the file isn't there to delete, same
    missing-target-is-an-error convention as every sibling delete in this
    codebase.
    """
    _validate_filename(filename)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        target = workdir / "bundle" / "tools" / filename
        if not target.is_file():
            raise ToolingError(f"tooling file not found: {filename}")
        target.unlink()
        github.commit_push(workdir, f"kobo: remove tools/{filename}")
        return github.local_head(workdir)


def import_tooling(
    github: GitHubClient,
    repo: str,
    *,
    source_repo: str,
    source_path: str,
    runtime_image: str | None = None,
) -> dict:
    """Clone `source_repo` (via the SAME `github` client the agent's own
    repo uses — its GitHub App identity already covers any org repo, so no
    separate client is needed), walk `source_path` NON-recursively for every
    `.py`/`.go` file, validate each one independently against the exact same
    `_validate_filename`/`_validate_content` a single upload uses, and push
    every file that passes into `repo`'s `bundle/tools/` in ONE commit.

    Returns `{"sha": str | None, "imported": [name, ...], "rejected":
    [{"name", "reason"}, ...]}`. One bad file never sinks the rest: a
    filename or content failure for one candidate is recorded in `rejected`
    (its `ToolingError` message, verbatim, as `reason`) and the remaining
    candidates are still validated and imported normally. `sha` is `None`
    (and NOTHING is pushed to `repo` — the agent repo is never even cloned)
    when nothing passed; there is nothing to commit, so there is nothing to
    re-pin the runtime image against either, matching the addendum's
    explicit "sha null when nothing passed".

    A `source_path` that doesn't exist in `source_repo` at all (or names a
    file, not a folder) is treated as zero candidates — the same
    empty-directory-reads-as-empty convention `kobo_api.intake.list_intake`
    already uses for a repo with no `bundle/intake/` at all — rather than a
    raised error: there is nothing malformed about the REQUEST in that case,
    just nothing there to import.

    `source_repo`/`source_path` are validated (owner/repo shape; no leading
    `/`, no `..` segment) BEFORE any clone at all — a malformed request
    never costs a network round trip. Raises `ToolingError` for either
    failure, same as a bad upload filename; the route maps this to a plain
    422, distinct from the per-file `rejected` entries below (those are a
    200 with partial results, not a request-level failure).

    Non-recursive by design (module docstring: "matching EVERY file in the
    folder", not the whole tree) — a subdirectory under `source_path` is
    silently not walked at all, neither imported nor rejected, since it was
    never a candidate to begin with.
    """
    _validate_source_repo(source_repo)
    _validate_source_path(source_path)

    with tempfile.TemporaryDirectory() as source_tmp:
        source_workdir = Path(source_tmp)
        github.clone(source_repo, source_workdir)
        source_dir = source_workdir / source_path

        if not source_dir.is_dir():
            raise ToolingError(f"tooling import path not found: {source_path} in {source_repo}")

        candidates: list[tuple[str, bytes]] = []
        for entry in sorted(source_dir.iterdir(), key=lambda p: p.name):
            if entry.is_file() and entry.suffix in _TOOLING_EXTENSIONS:
                candidates.append((entry.name, entry.read_bytes()))

        passed: list[tuple[str, bytes]] = []
        rejected: list[dict] = []
        for name, raw in candidates:
            try:
                _validate_filename(name)
                _validate_content(name, raw)
            except ToolingError as exc:
                rejected.append({"name": name, "reason": str(exc)})
                continue
            passed.append((name, raw))
        # source_workdir is discarded here (context exit) — every candidate
        # that's going anywhere has already been read into `passed` above.

    if not passed:
        return {"sha": None, "imported": [], "rejected": rejected}

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        tools_dir = workdir / "bundle" / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        for name, raw in passed:
            (tools_dir / name).write_bytes(raw)
        if runtime_image is not None:
            update_dockerfile_runtime_image(workdir / "Dockerfile", runtime_image)
        github.commit_push(workdir, f"kobo: import tools from {source_repo}/{source_path}")
        sha = github.local_head(workdir)

    return {
        "sha": sha,
        "imported": [name for name, _ in passed],
        "rejected": rejected,
    }


def _probe_enforcement(agent: AgentClient, namespace: str, deploy: str) -> dict:
    """Fetch the running pod's `/.well-known/agent-card.json` and derive the
    enforcement envelope `list_tooling` reports alongside its files:
    `{"attested": bool, "locked": bool, "locked_file": str | None}`.

    `attested` is True iff the card fetch succeeded AND its `enforcement`
    block's `version` is an int >= 1 — the runtime's own signal that it
    actually executes `bundle/tools/*` hooks around a reply, not merely
    stores them (Task 2's card-stamp contract). `locked`/`locked_file` are
    read straight from that same block (defaulting to `False`/`None` when
    absent) — a runtime can be attested (version >= 1) and still `locked`
    (a hook file failed to load, so it's serving refusals until fixed or
    removed), so the two are independent facts, not a single tri-state.

    ANY fetch or parse failure — an unreachable/still-booting pod (already
    `ClientError` from `AgentClient.fetch_card`), or a card whose
    `enforcement` value isn't the dict shape the runtime is documented to
    produce (`AttributeError`/`TypeError` from treating it as one anyway) —
    reads as the exact same "not attested" envelope. This is the module's
    honesty guarantee applied to the card the same way it's already applied
    to "delivered": never guess enforcement is live from a card kobo
    couldn't make sense of.
    """
    try:
        card = agent.fetch_card(namespace, deploy)
        enforcement_block = card.get("enforcement") or {}
        version = enforcement_block.get("version", 0)
        attested = isinstance(version, int) and not isinstance(version, bool) and version >= 1
        locked = bool(enforcement_block.get("locked", False))
        locked_file = enforcement_block.get("locked_file")
        return {"attested": attested, "locked": locked, "locked_file": locked_file}
    except (ClientError, AttributeError, TypeError):
        return {"attested": False, "locked": False, "locked_file": None}


def list_tooling(
    github: GitHubClient,
    kubectl: KubectlClient,
    agent: AgentClient,
    *,
    repo: str,
    namespace: str,
    deploy: str,
) -> tuple[list[dict], dict]:
    """The repo's current `bundle/tools/*` files, sorted by name, each
    `{"name", "content_bytes": int, "delivered": bool}`, alongside the
    enforcement envelope probed from the running pod's card (Task 4: card
    probe + banner graduation) — see `_probe_enforcement` for that shape and
    derivation.

    A repo with no `bundle/tools/` directory at all (no upload/import has
    ever happened) yields `([], enforcement)`, mirroring
    `kobo_api.intake.list_intake`'s same empty-dir convention — the
    enforcement probe still runs even then, since it's a property of the
    running agent, not of whether it has any tooling files at all.

    "delivered" is derived by the EXACT same ancestor-based reasoning as
    `list_intake`'s "ingested" (see that function's docstring for the full
    algorithm and the reasoning behind every step — repeated here, not
    imported, per the module docstring's note on why): clone once; for every
    tooling file, get its last-touch commit via
    `github.commit_shas_for_paths`; read the live Deployment's image tag via
    `kubectl.deploy_image` + `image_tag`; resolve that tag to a full commit
    sha inside THIS clone via `github.rev_parse_prefix` (`None` if it
    doesn't resolve); a file is "delivered" iff the tag resolved AND that
    commit is the file's last-touch commit or a descendant of it
    (`github.is_ancestor`, equality counts).

    This function never adds an "enforced" status to a file's own dict —
    "delivered" here still means only "reached a running pod", same as
    before Task 4. Deciding whether a delivered file is actually enforced
    (attested and not locked) is the HTTP route's job, composing this
    function's two return values (see `kobo_api.main`'s tooling list route).
    """
    enforcement = _probe_enforcement(agent, namespace, deploy)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        tools_dir = workdir / "bundle" / "tools"
        if not tools_dir.is_dir():
            return [], enforcement

        paths = sorted(
            (p for p in tools_dir.iterdir() if p.is_file()), key=lambda p: p.name
        )
        if not paths:
            return [], enforcement

        rel_paths = [f"bundle/tools/{p.name}" for p in paths]
        commit_shas = github.commit_shas_for_paths(workdir, rel_paths)

        image = kubectl.deploy_image(namespace, deploy)
        tag = image_tag(image)
        # None means a tagless digest-only image — every file below falls
        # through to "not delivered", the honest state (same as
        # list_intake's identical branch).
        live_commit = github.rev_parse_prefix(workdir, tag) if tag is not None else None

        results = []
        for path, rel_path in zip(paths, rel_paths):
            file_commit = commit_shas[rel_path]
            delivered = live_commit is not None and github.is_ancestor(
                workdir, file_commit, live_commit
            )
            results.append(
                {
                    "name": path.name,
                    "content_bytes": path.stat().st_size,
                    "delivered": delivered,
                }
            )
        return results, enforcement
