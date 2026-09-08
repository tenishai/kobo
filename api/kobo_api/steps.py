"""Provisioning steps: repo, onboard, access, wire, build, deploy.

Each class satisfies `kobo_api.provisioner.Step` (a `name` attribute plus
`run(ctx) -> str`) and is meant to run inside `run_pipeline`'s per-step
try/except boundary: raising from `run()` fails just that step (and aborts
everything after it), returning hands the pipeline a human-readable detail
string that isn't persisted anywhere today.

Resume story for both external-creation steps (repo, onboard) mirrors the
same shape: re-check the outside world's state rather than trusting only
what's in the DB row, and only treat a pre-existing thing as *this* agent's
prior work when the row already recorded it — otherwise raise, since a
same-named repo/app that kobo didn't create is not safe to adopt silently.

WireStep encodes the single highest-stakes finding from this project's live
P0 E2E: gojira's generator *preserves* existing `k8s/base/kustomization.yaml`
entries, it does not discover files on disk — an app-authored manifest that
isn't also registered there is silently never applied (the P0 run lost a
rollout to a missing ServiceAccount this way). Writing a manifest and
registering it in kustomization.yaml is therefore one atomic responsibility
here, never split across two steps or made optional.

WireStep's other invariant: it never completes without confirming gojira is
`active`, on every path — including the no-change resume path, where a prior
attempt may have crashed after calling `reprovision()` but before its own
`_wait_active` finished. Skipping that confirmation there would let a
retry mark the step DONE while gojira is still mid-provision (or has actually
failed), and BuildStep/DeployStep would then run against an unverified app.
"""

from __future__ import annotations

import base64
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import yaml

from kobo_api.avatars import avatar_prompt, set_avatar
from kobo_api.catalog import _DESCRIPTION_HEADER_RE
from kobo_api.clients import (
    CabalError,
    ClientError,
    GojiraConflict,
    StorageClient,
    StorageNotFound,
    _build_system_task_envelope,
    _build_task_envelope,
    _extract_relay_reply,
    cabal_enabled,
    gsa_email,
)
from kobo_api.colleagues import deliver_pass
from kobo_api.composer import BundleSpec, _colleagues_md, _self_md, compose, validate_agent_yaml
from kobo_api.config import Config, vertex_location_for
from kobo_api.directory import sync_directory
from kobo_api.metadata import (
    commit_card_metadata,
    generate_capability,
    skill_descriptions_for_agent,
    tooling_filenames_for,
)
from kobo_api.models import INVITE_ACCEPTED, A2AInvite, Agent, AgentStatus
from kobo_api.pairs import implicitly_paired
from kobo_api.purpose_tags import decode_tags
from kobo_api.search import get_entry, read_marketplace_catalog
from kobo_api.tooling import ToolingError, _validate_content, _validate_filename

log = logging.getLogger(__name__)

# Type-only: `kobo_api.provisioner` imports this module (for `PIPELINE`), so a
# runtime import here would be circular. `from __future__ import annotations`
# (above) already makes every annotation in this file a lazy string, so `Ctx`
# is only ever needed by type checkers, never at import time.
if TYPE_CHECKING:
    from kobo_api.clients import Clients, GitHubClient
    from kobo_api.provisioner import Ctx


class StepSkipped(Exception):
    """Raise from a `Step.run()` to mark that step SKIPPED rather than DONE
    or FAILED -- for a best-effort step whose own outcome must never block
    the rest of the pipeline, or the agent's own path to READY.

    `kobo_api.provisioner._run_pipeline_body` gives this its own except
    branch, ahead of the generic one every other exception falls into:
    caught here, the step's `AgentStep` row is recorded SKIPPED with this
    exception's message as the one-line reason, and the loop moves on to
    the next step exactly as if this one had succeeded -- unlike an
    uncaught exception, which fails the step, aborts every step after it,
    and lands the whole agent in FAILED. `AvatarStep` below is the first
    (and, as of this writing, only) step that raises this.
    """


# Gojira onboarding poll cadence: every 5s, up to 300s total (the brief's numbers,
# as module constants so both the step and its tests share one source of truth).
POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 300

# CI image-build poll cadence: every 10s, up to 600s total (BuildStep's own
# numbers — distinct constants from the gojira-onboarding ones above since the
# two poll different backends on different cadences).
BUILD_POLL_INTERVAL_S = 10
BUILD_POLL_TIMEOUT_S = 600

# Cap on how many times `head_sha` can advance (change between polls) during a
# single BuildStep invocation. The pipeline pushes at most twice per provision
# (WireStep's push + the platform's auto-merged setup PR landing), so at most
# one advance is ever expected — a second advance indicates an unexpected
# sequence of concurrent pushes and is treated as a bounded failure (raises
# naming the observed shas) rather than looping indefinitely.
BUILD_MAX_HEAD_ADVANCES = 2

# AvatarStep's size cap on a generated PNG before it's pushed/cached -- same
# 4 MiB figure, and the same rationale, as `kobo_api.main.AVATAR_MAX_BYTES`
# (the human-facing `POST .../avatar` route this step mirrors). Duplicated
# rather than imported: `kobo_api.main` imports FROM this module (routes
# wire steps, not the other way around), so importing the constant back
# from there would be circular -- same story as this file's own
# `StepSkipped`/`Ctx` type-only imports above.
AVATAR_MAX_BYTES = 4 * 1024 * 1024

# Grace budget for OnboardStep's post-reprovision status read (live-incident
# fix #2, follow-up to the reprovision fix above `_wait_active`). Deliberately
# NOT POLL_TIMEOUT_S // POLL_INTERVAL_S — that budget belongs to
# `_wait_active`'s much longer "wait for provisioning to actually finish"
# loop; this one only answers the narrower question "did the new run start
# yet", so a small, dedicated count is both sufficient and keeps the bound
# tight. See the call site for the incident this closes.
ONBOARD_REPROVISION_GRACE_POLLS = 6

# After BuildStep fires its one self-heal rerun, gh's API can briefly still
# echo the PRE-rerun completed/non-success payload (the same read-after-write
# lag BuildStep already accounts for on `head_sha`) — this many polls of
# grace are given to that specific run id before a completed-non-success
# verdict is trusted as a genuine second failure. A fixed poll count rather
# than "wait for an observed non-completed status" deliberately: it doesn't
# depend on ever seeing an intermediate queued/in_progress read (which a
# fast-cycling rerun, or a test fake, may never produce), and it's still
# bounded — worst case adds this many polls before either a real result
# lands or the second-failure raise fires.
BUILD_RERUN_STALE_GRACE_POLLS = 3

# HelloStep's fresh-identity propagation window (live incident 2026-08-27): a
# brand-new agent's minutes-old service account and workload-identity binding
# can still be propagating when first contact is made, surfacing as a
# transient ClientError/CabalError (observed as a google-auth 403 minting a
# token, relayed to the operator as a 502) that a manual Retry always clears
# untouched. Three attempts, ~20s apart, absorb that window automatically
# instead of leaving it to a human click.
HELLO_RETRY_ATTEMPTS = 3
HELLO_RETRY_BACKOFF_S = 20

# Module-level sleep seam: tests monkeypatch this to a no-op so a 300s/5s (or
# 600s/10s) poll loop — or a deliberately-triggered timeout, which walks the
# whole loop — never actually blocks. Referenced as a bare name inside each
# step's run() so the monkeypatch (which rebinds this module attribute) is
# visible at call time.
_sleep = time.sleep


def _resolve_persona_body(
    config: Config, storage: StorageClient, engine, agent: Agent
) -> str:
    """Read `agent.persona`'s body straight from the personas bucket (bucket
    catalogs, Task 5) and substitute the agent's own name into it.

    `f"{agent.persona}/persona.md"`: personas are org-wide and flat under
    their id in the bucket (see `kobo_api.personas.create_persona`'s own key
    shape, `f"{prefix}{id_}/persona.md"`) — no team segment needed to find
    one. `engine` is accepted for parity with this step's other bucket-
    catalog callers (and room for a future FTS cross-check) but isn't needed
    for this direct-by-id bucket read.

    Strips the leading `<!-- description: ... -->` header line if present
    (the same convention `kobo_api.catalog._parse_persona_file` parses off
    the packaged/room-authored bodies), then substitutes the agent's name via
    plain `.replace` — never `.format`, which would choke on (or
    misinterpret) any other literal `{...}` the persona's own purpose/
    boundaries text might carry (the ledgered P1a brace hazard this closes).
    """
    key = f"{agent.persona}/persona.md"
    try:
        raw = storage.read_bytes(config.personas_bucket, key)
    except StorageNotFound as exc:
        raise RuntimeError(
            f"persona {agent.persona!r} (recorded on agent {agent.name!r}) is not "
            "in the persona catalog"
        ) from exc

    text = raw.decode()
    newline = text.find("\n")
    first_line = text[:newline] if newline != -1 else text
    if _DESCRIPTION_HEADER_RE.match(first_line):
        body = text[newline + 1 :] if newline != -1 else ""
    else:
        body = text
    return body.replace("{name}", agent.name)


@dataclass
class StagedSkill:
    id: str
    dir: Path


def _materialize_marketplace_skill(
    storage: StorageClient, config: Config, skill_id: str, entry: dict, dest_dir: Path
) -> str:
    """Download one marketplace-dialect skill's `SKILL.md` into `dest_dir`
    (persona-metadata plan Task 1b).

    Prefers the immutable version-pinned path (`catalog/<id>/versions/
    <version>/SKILL.md`) whenever the catalog entry names a `version`;
    falls back to the mutable head copy (`catalog/<id>/SKILL.md`) only when
    the entry carries no version at all, logging a warning naming the
    skill -- that fallback is a signal worth an operator's attention, not a
    silent, routine choice. Returns the entry's own `content_hash` (empty
    string when absent) as the sha a caller records as this pick's pin --
    the marketplace dialect has no per-object custom metadata to read the
    way the legacy dialect's `source-sha` does. Raises RuntimeError,
    mirroring the legacy path's vanished-skill contract, if even the
    resolved path is gone.
    """
    version = entry.get("version")
    if version:
        key = f"catalog/{skill_id}/versions/{version}/SKILL.md"
    else:
        log.warning(
            "marketplace catalog entry for skill %r carries no version -- "
            "falling back to the mutable head copy at catalog/%s/SKILL.md",
            skill_id,
            skill_id,
        )
        key = f"catalog/{skill_id}/SKILL.md"

    try:
        content = storage.read_bytes(config.skills_bucket, key)
    except StorageNotFound as exc:
        raise RuntimeError(f"skill no longer in the catalog: {skill_id}") from exc

    (dest_dir / "SKILL.md").write_bytes(content)
    return entry.get("content_hash", "")


def _materialize_catalog_skill(
    storage: StorageClient,
    config: Config,
    team: str,
    skill_id: str,
    dest_dir: Path,
    marketplace_catalog: dict[str, dict] | None = None,
) -> str:
    """Download the catalog skill `<team>/<skill_id>/` into dest_dir.

    `marketplace_catalog`, when not None, is the bucket's marketplace-
    dialect `skills` mapping (`kobo_api.search.read_marketplace_catalog`,
    read once by the caller, `_stage_skills`, rather than re-reading it per
    skill). A skill present there is fetched via
    `_materialize_marketplace_skill` instead of the legacy team-prefixed
    directory listing below -- that dialect has no team segment in its
    storage layout at all, so `team` plays no part in resolving its
    content. A marketplace-dialect bucket that doesn't carry this
    particular skill_id falls through to the legacy listing below, which
    naturally raises the same vanished-skill error an empty `list_keys`
    already produces today.

    Same containment rule as before the extraction: a bucket key must not
    escape dest_dir. Returns the SKILL.md object's current source-sha
    custom metadata ("" when the publisher set none). Raises RuntimeError
    when the catalog no longer serves the skill.
    """
    if marketplace_catalog is not None:
        entry = marketplace_catalog.get(skill_id)
        if entry is not None:
            return _materialize_marketplace_skill(storage, config, skill_id, entry, dest_dir)

    prefix = f"{team}/{skill_id}/"
    keys = storage.list_keys(config.skills_bucket, prefix)
    if not keys:
        raise RuntimeError(f"skill no longer in the catalog: {team}/{skill_id}")
    resolved_dest = dest_dir.resolve()
    for key in keys:
        relative = key[len(prefix):]
        if not relative:
            continue
        target = dest_dir / relative
        if not target.resolve().is_relative_to(resolved_dest):
            raise RuntimeError(
                f"skill {skill_id!r} (team {team!r}) bucket key {key!r} "
                f"would download outside its staging directory; refusing to write it"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(storage.read_bytes(config.skills_bucket, key))
    try:
        metadata = storage.object_metadata(config.skills_bucket, f"{prefix}SKILL.md")
    except StorageNotFound:
        metadata = {}
    return metadata.get("metadata", {}).get("source-sha", "")


def _stage_skills(
    storage: StorageClient, config: Config, agent: Agent, staging: Path
) -> list[StagedSkill]:
    """Download every one of `agent.skills`'s entries (each a `{"name": ...,
    "team": ..., "sha": ...}` dict, the shape the DB column holds) from the
    skills bucket into `staging/<id>/`, preserving each object's relative
    path under the bucket's `<team>/<id>/` prefix.

    A skill (or its whole team) that's vanished from the catalog since this
    agent picked it (`storage.list_keys` returns no keys under that prefix)
    is a hard failure, not a silent drop — the composed bundle would
    otherwise ship without a skill the operator believes it has.

    Sha drift: `SKILL.md`'s own `source-sha` custom metadata (set by
    whatever publishes the skills bucket) is compared against the sha
    recorded on the agent row at pick time. A mismatch means the catalog
    entry has moved on since the pick — kobo has only ever kept the ONE
    current copy of a skill's tree in the bucket (unlike the old skills-repo
    git-archive path, which could still resolve an older commit), so there is
    no older content left to actually pin to. The honest move is to proceed
    with what's actually there now and correct the recorded pin to match,
    logged at warning — never to fail the whole provision over a catalog
    that simply moved between pick and assembly. `agent.skills` is
    reassigned to a fresh list (rather than mutating an entry dict in place)
    so the caller's session actually sees the JSON column as dirty and
    commits the correction — mutating a dict already inside the existing
    list would not be detected by SQLAlchemy's change tracking.

    Marketplace dialect (persona-metadata plan Task 1b): the skills
    bucket's dialect probe (`kobo_api.search.read_marketplace_catalog`) is
    read exactly once here, up front, rather than once per skill -- a
    single small JSON read for the whole call, reused by every
    `_materialize_catalog_skill` invocation below.
    """
    staged: list[StagedSkill] = []
    resolved_entries: list[dict] = []
    drifted = False
    marketplace_catalog = read_marketplace_catalog(storage, config.skills_bucket)

    for entry in agent.skills:
        if entry.get("source") == "local":
            # Local skills live in the repo bundle already; staging is
            # catalog-only. No current path reaches this (compose only runs
            # pre-repo-creation, before a local skill could exist), but a
            # local entry carries no "team" key, so falling through to the
            # lookups below would KeyError.
            resolved_entries.append(entry)
            continue

        skill_id = entry["name"]
        team = entry["team"]
        recorded_sha = entry.get("sha") or ""

        skill_dir = staging / skill_id
        skill_dir.mkdir(parents=True, exist_ok=True)
        current_sha = _materialize_catalog_skill(
            storage, config, team, skill_id, skill_dir, marketplace_catalog
        )

        if recorded_sha and current_sha and current_sha != recorded_sha:
            log.warning(
                "skill %r (team %r) drifted since agent %r picked it — recorded "
                "sha %r, catalog now at %r; shipping current content and "
                "updating the recorded pin",
                skill_id, team, agent.name, recorded_sha, current_sha,
            )
            entry = {**entry, "sha": current_sha}
            drifted = True

        resolved_entries.append(entry)
        staged.append(StagedSkill(id=skill_id, dir=skill_dir))

    if drifted:
        agent.skills = resolved_entries

    return staged


def _stage_baseline_tools(
    github: GitHubClient, config: Config, bundle_dir: Path
) -> list[str]:
    """Copy the org marketplace's baseline tooling pack into
    `bundle_dir/tools/`, returning the staged filenames (sorted).

    The pack is every `tools/<id>/<id>.py` in the marketplace repo whose id
    starts with `marketplace.BASELINE_TOOL_PREFIX` -- matched by that shape
    exactly, so a non-baseline tool, a `TOOL.md`, and a stray helper module
    sitting beside a baseline tool's own entrypoint are all left behind.
    Files land FLAT in `bundle_dir/tools/` under their own basename (the
    layout `kobo_api.tooling.upload_tooling_file` and the runtime's
    enforcement envelope both already expect), never as the per-tool
    subdirectory the marketplace stores them in.

    Every candidate is validated INDEPENDENTLY against the exact same
    `kobo_api.tooling` checks (`_validate_filename`/`_validate_content`:
    size/UTF-8/non-empty/ast/import-allowlist/entrypoint) a single upload
    runs, and a `ToolingError` skips that one file -- mirroring
    `tooling.import_tooling`'s per-file posture. This is not belt-and-braces:
    the marketplace publishes by DIRECT COMMIT with no review gate
    (`kobo_api.marketplace`'s module docstring), so one syntactically broken
    or non-stdlib-importing file really can be sitting in that tree, and
    staging it would ship a bundle the runtime's enforcement loader LOCKS --
    turning one team's bad push into a failed create for every new agent in
    the room. One bad file costs that file only.

    Containment, on the same "this is a shared repo nobody reviews" footing:
    every source path must still resolve INSIDE the clone (so a symlink out
    of the tree can't smuggle content off the cloning machine into an
    agent's bundle), and the destination is always `dest_dir / src.name` --
    a single component from a real directory listing -- so no path in that
    repo can steer a write outside `bundle_dir`.

    Nothing is written until every candidate has been read and validated:
    a failure partway through collection therefore stages nothing at all,
    keeping the "skipped" the warning reports literally true.

    A room with no `skills_repo` configured at all returns `[]` silently --
    it has no marketplace, so there is no pack to miss and nothing worth
    warning about on every single create. Past that point ANY failure is
    swallowed into ONE warning and `[]`/a partial list: a clone that won't
    complete, an unreadable file, a rejected candidate. An agent must not
    fail to exist because the marketplace hiccuped -- the same best-effort
    posture `kobo_api.metadata.tooling_filenames_for` takes for its own
    github read. The pack is a head start, not a precondition; a room that
    wanted it and didn't get it can add the tools through `POST .../tooling`
    after birth.
    """
    # Imported here, not at module scope: `kobo_api.marketplace` imports
    # `kobo_api.skills`, which imports THIS module (`_materialize_catalog_
    # skill`) -- a top-level import would be a cycle that fails at import
    # time, since that name is defined below where the import would sit.
    from kobo_api.marketplace import BASELINE_TOOL_PREFIX, marketplace_repo

    if not config.skills_repo:
        return []

    repo = config.skills_repo
    skipped: list[str] = []
    try:
        repo = marketplace_repo(config)
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            github.clone(repo, workdir)
            resolved_clone = workdir.resolve()
            pattern = f"{BASELINE_TOOL_PREFIX}*/{BASELINE_TOOL_PREFIX}*.py"

            collected: list[tuple[str, bytes]] = []
            for src in sorted((workdir / "tools").glob(pattern)):
                if not src.is_file():
                    skipped.append(f"{src.name} (not a regular file)")
                    continue
                if not src.resolve().is_relative_to(resolved_clone):
                    skipped.append(f"{src.name} (resolves outside the marketplace clone)")
                    continue
                raw = src.read_bytes()
                try:
                    _validate_filename(src.name)
                    _validate_content(src.name, raw)
                except ToolingError as exc:
                    skipped.append(f"{src.name} ({exc})")
                    continue
                collected.append((src.name, raw))

        # Collection is complete and the clone is gone: writing now means a
        # failure while READING the marketplace can never leave a half-staged
        # pack behind (see the docstring).
        dest_dir = bundle_dir / "tools"
        for name, raw in collected:
            # mkdir on the first real file, not up front: a room whose
            # marketplace carries no pack at all gets no empty
            # `bundle/tools/` directory in its bundle either.
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / name).write_bytes(raw)
    except Exception as exc:  # noqa: BLE001 - never fatal, see the docstring
        log.warning("baseline tooling skipped for %s: %s", repo, exc)
        return []

    if skipped:
        log.warning("baseline tooling skipped for %s: %s", repo, "; ".join(skipped))
    return [name for name, _raw in collected]


def _colleague_roster(session, agent: Agent) -> list[dict]:
    """The room's own view of who `agent` may reach, for the composed
    bundle's standing colleague knowledge (Task 5). Same underlying rule as
    `kobo_api.pairs.allowed_pairs` -- same non-`SHARED_GROUP` group (via the
    one shared predicate, `implicitly_paired`), plus an ACCEPTED invite
    where `agent` is the inviter -- but `allowed_pairs` itself isn't
    reusable here: it only ever considers READY agents as callers, and
    `agent` is never READY yet at compose time (RepoStep runs early in the
    provisioning pipeline, well before the READY flip). Only READY agents
    are considered as TARGETS, same as `allowed_pairs`: a colleague this
    agent can't actually dispatch to yet (still provisioning, or failed)
    doesn't belong in a "who you can reach" roster.

    Important 6 fix: `skills` is now populated too -- `composer.py`'s
    `_colleague_capability_line` falls back capability -> purpose_tags ->
    skills -> "not yet distilled", but this dict carried no "skills" key at
    all, so that third fallback level was permanently unreachable no matter
    how bare a colleague's roster entry was. Sourced from `other.skills`,
    the same JSON-column shape `_stage_skills` documents (a list of
    `{"name": ..., "team": ..., "sha": ...}` dicts, or a `{"source":
    "local", ...}` entry for a local skill) -- just the names, pick order
    preserved, never re-sorted (the fallback only ever joins them into one
    line; pick order is as meaningful an order as any).
    """
    ready_others = (
        session.query(Agent)
        .filter(Agent.status == AgentStatus.READY, Agent.name != agent.name)
        .all()
    )
    accepted_targets = {
        invite.to_agent
        for invite in session.query(A2AInvite)
        .filter(A2AInvite.from_agent == agent.name, A2AInvite.status == INVITE_ACCEPTED)
        .all()
    }
    # Sorted by name so the composed colleagues.md is deterministic:
    # DB order varies run to run, and a nondeterministic bundle file makes
    # every recompose look like a content change in the repo diff.
    roster = []
    for other in sorted(ready_others, key=lambda a: a.name):
        if implicitly_paired(agent.group_name, other.group_name) or other.name in accepted_targets:
            roster.append(
                {
                    "name": other.name,
                    "team": other.team,
                    "purpose_tags": decode_tags(other.purpose_tags),
                    "capability": other.capability,
                    "skills": [
                        entry["name"] for entry in (other.skills or []) if entry.get("name")
                    ],
                }
            )
    return roster


# --- Roster refresh lane (fast-lane + discovery arc, Task 6) -----------------
#
# `_colleague_roster` above answers "who can THIS agent reach" at compose
# time, once, at birth. The functions below are its mirror image, answering
# "who can reach THIS agent" at any later point, so that when the room's own
# view of an agent changes (it goes READY, it is destroyed, its capability
# distills), every OTHER agent who could already see it gets a freshly
# rendered `colleagues.md` over the fast lane instead of carrying a stale one
# until its own next rebuild -- spec `docs/superpowers/specs/2026-08-26-fast-
# lane-and-discovery-design.md` section 3.3.


def _agents_who_can_see(session, *, name: str, group_name: str | None) -> list[Agent]:
    """The reverse of `_colleague_roster`'s own visibility rule: every READY
    agent, other than `name` itself, that would list `name` in its own
    colleague roster right now. Same two-part predicate `_colleague_roster`
    applies (implicit group pairing via `implicitly_paired`, or an ACCEPTED
    invite the viewer itself sent TO `name`) -- reused verbatim, just walked
    in the other direction: the viewer varies here, `name`/`group_name` are
    the fixed anchor, rather than the other way around. No second visibility
    rule is written; this is the same one, inverted.

    Takes `name`/`group_name` as plain values rather than an `Agent`
    instance, deliberately: a destroy caller must ask this question using
    the departing agent's own name/group captured BEFORE its status leaves
    READY. This function never reads the target's own status at all (only
    compares against it), which is exactly why it stays correct even after
    that flip -- `_colleague_roster` itself only ever lists READY TARGETS,
    so calling it (or checking membership in its output) after the flip
    would wrongly report that nobody could see the departing agent, when a
    moment earlier several agents could.
    """
    candidates = (
        session.query(Agent)
        .filter(Agent.status == AgentStatus.READY, Agent.name != name)
        .all()
    )
    accepted_from = {
        invite.from_agent
        for invite in session.query(A2AInvite)
        .filter(A2AInvite.to_agent == name, A2AInvite.status == INVITE_ACCEPTED)
        .all()
    }
    return [
        viewer
        for viewer in candidates
        if implicitly_paired(viewer.group_name, group_name) or viewer.name in accepted_from
    ]


def _relay_reply_not_completed(raw: object) -> bool:
    """True unless `raw` is a well-formed relayed task whose status state is
    exactly "completed". Deliberately duplicated from `kobo_api.main`'s
    identically-behaved `_teach_task_not_completed` rather than imported --
    that function is a closure nested inside `create_app` (not reachable
    from this module), and its own docstring already establishes the
    precedent that this specific check is meant to be copied to each new
    caller rather than shared, the same way `_extract_relay_reply` is
    shared but this narrower bool check is not."""
    task: object = raw
    if isinstance(raw, dict):
        if isinstance(raw.get("task"), dict):
            task = raw["task"]
        elif isinstance(raw.get("result"), dict):
            task = raw["result"]

    if not isinstance(task, dict):
        return True

    status = task.get("status")
    if not isinstance(status, dict):
        return True

    return status.get("state") != "completed"


def _render_colleagues_md(viewer: Agent, roster: list[dict]) -> str:
    """Render `colleagues.md` for `viewer` from `roster` (its own freshly
    computed `_colleague_roster`), via the identical composer function that
    renders it at birth (`kobo_api.composer._colleagues_md`) -- so a
    redelivered file is byte-for-byte what a fresh compose would produce
    for the same roster. Every `BundleSpec` field below other than `name`/
    `colleagues` is unused by `_colleagues_md` (confirmed by reading it) and
    filled with a throwaway placeholder purely to satisfy the dataclass's
    required fields; none of it is ever rendered."""
    spec = BundleSpec(
        name=viewer.name,
        persona_body="",
        model_id=viewer.model_id,
        skills=[],
        runtime_image="",
        vertex_project="",
        vertex_location="",
        colleagues=roster,
    )
    return _colleagues_md(spec)


def _render_self_md(agent: Agent) -> str:
    """Render `self.md` for `agent` from its OWN current row, via the
    identical composer function that renders it at birth (`kobo_api.
    composer._self_md`) -- so a redelivery after capability distills is
    byte-for-byte what a fresh compose would produce. Same throwaway-
    placeholder posture as `_render_colleagues_md` for every `BundleSpec`
    field `_self_md` never reads."""
    spec = BundleSpec(
        name=agent.name,
        persona_body="",
        model_id=agent.model_id,
        skills=[],
        runtime_image="",
        vertex_project="",
        vertex_location="",
        team=agent.team or "shared",
        group=agent.group_name,
        purpose_tags=decode_tags(agent.purpose_tags),
        capability=agent.capability,
    )
    return _self_md(spec)


def _deliver_knowledge_refresh(
    clients: Clients, target_name: str, filename: str, content: str
) -> bool:
    """One best-effort `knowledge` system-task delivery of `content` under
    `filename` to `target_name`'s own switchboard channel -- the shared
    delivery mechanics both `refresh_colleague_rosters` (per affected
    viewer, `filename="colleagues.md"`) and the capability-distill self.md
    redelivery (`filename="self.md"`) ride. The runtime's own retire-then-
    store semantics for a `knowledge` task REPLACE the file's prior entries
    with this render, a clean replace rather than an append.

    Returns True on a confirmed-completed reply, False on ANY miss (a
    transport error, a non-2xx `CabalError`, or a well-formed-but-not-
    completed reply) -- logged here, never raised, so one target's miss
    never stops a caller from trying the next one. Global Constraints (Task
    6): this is the ONE fast-lane caller in this room with no rebuild
    fallback on a miss -- see `refresh_colleague_rosters`'s docstring for
    why that is the correct discipline here specifically.

    Callers are responsible for the `cabal_enabled(config) and clients.
    cabal is not None` gate; this function assumes `clients.cabal` is
    already wired.
    """
    envelope = _build_system_task_envelope(
        "knowledge", content, metadata={"filename": filename}
    )
    try:
        raw = clients.cabal.relay(target_name, envelope)
        if _relay_reply_not_completed(raw):
            raise CabalError("agent reply was not a completed task")
        _extract_relay_reply(raw)
    except (CabalError, httpx.HTTPError, ValueError) as exc:
        log.warning(
            "roster refresh relay failed for agent %s (%s): %s", target_name, filename, exc
        )
        return False
    return True


def refresh_colleague_rosters(
    clients: Clients, config: Config, session, *, name: str, group_name: str | None
) -> None:
    """Best-effort roster refresh (Task 6, the roster refresh lane): when
    `name`'s presence in the room changes (it just went READY, it is
    departing via destroy-forget, or its capability just distilled),
    redeliver a freshly rendered `colleagues.md` to every agent that can
    currently see it (`_agents_who_can_see`, the same visibility rule
    `_colleague_roster` itself applies, reversed) -- never to every agent in
    the room (spec 3.3's scope discipline: "a room with N agents does not
    redeliver to all N on every change").

    Callers pass `name`/`group_name` as plain values, not an `Agent`
    instance, matching `_agents_who_can_see`'s own contract -- a destroy
    caller must capture the departing agent's name/group BEFORE its status
    leaves READY (see that function's docstring for why).

    BEST-EFFORT ONLY, over the relay, with NO rebuild fallback: unlike the
    knowledge/teach/tooling fast lanes, a miss here is logged and skipped,
    never escalated to a repo commit plus CI rebuild. A stale roster
    already self-describes as a point-in-time snapshot (`_colleagues_md`'s
    own disclaimer text), and paying a full commit-plus-CI-rebuild for a
    single roster line changing is exactly the cost this refresh lane
    exists to remove -- falling back to one here would silently reintroduce
    it. One viewer's miss never blocks refreshing the rest.

    A no-op when the switchboard isn't configured/wired (`cabal_enabled
    (config) and clients.cabal is not None`, the same two-gate every other
    fast-lane caller in this room checks) -- no relay is even attempted.
    """
    if not (cabal_enabled(config) and clients.cabal is not None):
        return
    for viewer in _agents_who_can_see(session, name=name, group_name=group_name):
        content = _render_colleagues_md(viewer, _colleague_roster(session, viewer))
        _deliver_knowledge_refresh(clients, viewer.name, "colleagues.md", content)


def refresh_after_capability_distill(
    clients: Clients, config: Config, session, agent: Agent
) -> None:
    """Both refresh halves spec 3.3 names for the capability-distill
    trigger: `agent`'s regenerated view reaches every viewer who can see it
    (`refresh_colleague_rosters` -- the colleagues.md line naming `agent`'s
    capability just changed for each of them), AND `agent`'s own `self.md`
    is redelivered to itself, so its self-knowledge does not stay frozen at
    "not yet distilled" forever once the real sentence exists.

    Same best-effort, no-rebuild-fallback, switchboard-gated discipline as
    `refresh_colleague_rosters` for both halves -- see its docstring.
    """
    refresh_colleague_rosters(
        clients, config, session, name=agent.name, group_name=agent.group_name
    )
    if not (cabal_enabled(config) and clients.cabal is not None):
        return
    _deliver_knowledge_refresh(clients, agent.name, "self.md", _render_self_md(agent))


class RepoStep:
    """Composes the agent's daedalus bundle and pushes it as a new GitHub repo."""

    name = "repo"
    label = "Repository"

    def run(self, ctx: Ctx) -> str:
        agent = ctx.agent
        config = ctx.config
        github = ctx.clients.github
        storage = ctx.clients.storage
        engine = ctx.session.get_bind()

        repo = f"{config.github_org}/agent-{agent.name}"

        if github.repo_exists(repo):
            if agent.repo_full_name == repo:
                # Idempotent resume: a prior attempt already created and
                # pushed this repo (e.g. it crashed/failed after
                # create_repo_from_dir succeeded but before this run()
                # returned). GitHub already has the composed bundle, so
                # there's nothing left to do — skip compose/validate/create
                # entirely rather than recomposing into a dir that may still
                # have a pushed git history, or calling create_repo_from_dir
                # against a repo that already exists (which would fail).
                return f"already created: {repo}"
            raise RuntimeError(
                f"repo {repo!r} already exists and is not recorded for agent "
                f"{agent.name!r} — refusing to adopt it"
            )

        persona_body = _resolve_persona_body(config, storage, engine, agent)

        staging_dir = ctx.workdir / "staging"
        # Retry safety (mirrors compose_dir's own rmtree below): a prior
        # attempt may have left a partial staging download behind.
        shutil.rmtree(staging_dir, ignore_errors=True)
        staged_skills = _stage_skills(storage, config, agent, staging_dir)

        spec = BundleSpec(
            name=agent.name,
            persona_body=persona_body,
            model_id=agent.model_id,
            skills=staged_skills,
            runtime_image=agent.runtime_image or config.runtime_image,
            vertex_project=config.vertex_project,
            vertex_location=vertex_location_for(config, agent.model_id),
            # Persona metadata (Task 3): thread the agent row's own team
            # (Task 2's own value, "shared" being the same default
            # CreateAgentRequest.team already falls back to) and purpose_tags
            # through to the baked agent-card.json's metadata block.
            team=agent.team or "shared",
            # Access control (access-control arc, Task A2): the agent row's
            # own ACL namespace (`Agent.group_name`'s own docstring: DISTINCT
            # from `team` above, "the two must never be conflated") threaded
            # through untouched so `_self_md` can report it as its own,
            # separately-labelled fact rather than reusing `team`'s value.
            group=agent.group_name,
            purpose_tags=decode_tags(agent.purpose_tags),
            # Switchboard (cabal) base URL (Task 4): when configured, gates
            # the a2a auth block at compose time — presence of cabal_url is
            # the composer's own gate for whether the transport carries
            # auth credentials (issuer/audience/jwks_url).
            cabal_url=config.cabal_url,
            # Self-card knowledge (standing-knowledge arc, Task 4): almost
            # always None here -- capability is "earned" later, well after
            # this first (and only) compose, by regenerate_agent_metadata --
            # but threaded rather than left unset so a future second compose
            # path doesn't silently drop it.
            capability=agent.capability,
            # Colleague roster (standing-knowledge arc, Task 5): the room's
            # own view of who this agent may reach, computed fresh at
            # compose time (see `_colleague_roster`'s own docstring for why
            # `kobo_api.pairs.allowed_pairs` itself isn't reusable here).
            colleagues=_colleague_roster(ctx.session, agent),
            # Agent memory replication (resilience arc, Task 14): threaded
            # straight from org config -- empty means off, and `compose()`
            # emits no supervision at all in that case (see BundleSpec's own
            # field docstring).
            memory_replica_bucket=config.memory_replica_bucket,
            # Agent PR broker (2026-08-27 spec): the room's own in-cluster
            # URL, threaded the same way as every other org coordinate --
            # empty means the composed bundle carries no github block.
            broker_url=config.broker_url,
            liminal_url=config.liminal_url,
        )

        compose_dir = ctx.workdir / "repo"
        # Retry safety: compose() raises FileExistsError if dest already has
        # the skills subtree (a prior attempt may have composed but failed
        # before/at create_repo_from_dir) — clear it before recomposing.
        shutil.rmtree(compose_dir, ignore_errors=True)
        compose(spec, compose_dir)

        # Baseline tooling pack (Task 6): staged into the just-composed bundle
        # rather than up beside `_stage_skills` above, for one mechanical
        # reason -- the rmtree two lines up would wipe anything written into
        # compose_dir before compose() ran. It still runs before
        # validate_agent_yaml/create_repo_from_dir, so the pack is part of the
        # very first push, never a follow-up commit. The return value (the
        # staged filenames) is deliberately unused: the composed tree itself
        # is what ships, and `bundle/tools/` in the repo is already the one
        # source of truth for what tools an agent carries.
        if not agent.skip_baseline:
            _stage_baseline_tools(github, config, compose_dir / "bundle")

        validate_agent_yaml(compose_dir / "bundle")

        github.create_repo_from_dir(repo, compose_dir)
        agent.repo_full_name = repo
        return f"created {repo}"


class AvatarStep:
    """A face at birth: generates the agent's avatar and pushes it to its
    just-created repo, mirroring `kobo_api.main.generate_avatar`'s own
    sequence (persona-description lookup, `clients.image.generate`, the
    `AVATAR_MAX_BYTES` size cap, `set_avatar`, the `Agent.avatar_png`
    base64 cache) almost exactly -- the one difference being what happens
    when something goes wrong.

    That route answers a human's button-press, so its failures are a 502/
    503 the human can just retry. This step runs unattended, inside the
    one-click pipeline, immediately after `RepoStep` -- and a picture is a
    nice-to-have, never a reason to leave an otherwise-healthy agent stuck
    in FAILED. So EVERY failure path here -- no image backend configured,
    `clients.image.generate` raising, the generated PNG coming back over
    the size cap, the repo push itself failing, or the final `avatar_png`
    encode/persist failing -- raises `StepSkipped` with a one-line reason
    instead of letting the underlying exception propagate. A persona-
    description or purpose-tags lookup failure is guarded too, but
    degrades quietly to a generic, tag-free prompt rather than skipping --
    a missing/unreadable persona is never worth losing the picture over.
    `StepSkipped` is exactly what keeps this best-effort: `kobo_api.
    provisioner._run_pipeline_body` records that as SKIPPED, not FAILED,
    and -- unlike a real failure -- moves straight on to the next step
    rather than aborting the rest of the pipeline.

    Idempotent resume, belt-and-suspenders: a pre-existing `agent.
    avatar_png` (this step already ran and succeeded on a prior attempt, or
    a human already generated/uploaded one via the routes above before this
    ever got a turn) short-circuits to `StepSkipped` before touching
    `clients.image` or the repo at all -- the engine's own DONE/SKIPPED
    resume check (`_get_or_create_step`) already prevents a second run of
    this exact step, but checking the agent's own state here too means a
    stray re-invocation can never overwrite a picture that's already there.
    """

    name = "avatar"
    label = "Paint the portrait"

    def run(self, ctx: Ctx) -> str:
        agent = ctx.agent
        clients = ctx.clients

        if agent.avatar_png is not None:
            raise StepSkipped("agent already has a picture")

        if clients.image is None:
            raise StepSkipped("picture generation is not available")

        # Same catalog-lookup-by-persona-id idiom as `generate_avatar`'s own
        # (kobo_api.main), with the same id-derived fallback for a persona
        # that's since been removed from the catalog. Unlike that route,
        # this never calls `_maybe_refresh_catalogs` first -- that's an
        # app-level TTL/background-thread concern this step has no `app` to
        # reach, and a stale or empty catalog here just means the honest
        # generic fallback below, never a reason to skip the picture
        # entirely.
        #
        # Wrapped broader than just the "not in the catalog" branch below:
        # `get_entry` itself can raise (a catalog/search backend hiccup),
        # and `decode_tags` raises on a malformed `purpose_tags` column --
        # either one, left unguarded, would propagate straight out of this
        # step and into `_run_pipeline_body`'s generic FAILED branch
        # (kobo_api.provisioner), turning a picture problem into a raw
        # exception on the whole agent. Both degrade to the exact same
        # generic, tag-free prompt a plain unresolved persona already falls
        # back to.
        engine = ctx.session.get_bind()
        try:
            persona_entry = get_entry(engine, "persona", agent.persona, team=None)
            if persona_entry is not None:
                persona_description = persona_entry.description
            else:
                log.warning(
                    "persona %r not in the catalog, using generic text for avatar prompt",
                    agent.persona,
                )
                persona_description = f"A {agent.persona} agent."
            tags = decode_tags(agent.purpose_tags)
        except Exception as exc:  # degrade to the generic fallback, never fail the agent
            log.warning(
                "avatar step %s for %s: %s", "persona/tags lookup", agent.name, exc
            )
            persona_description = f"A {agent.persona} agent."
            tags = []

        prompt = avatar_prompt(agent.name, persona_description, tags)

        # Broader than the route's own `except ClientError`: this step's
        # whole job is to never let a picture problem become an agent
        # problem, so any exception from the image backend here -- not just
        # the ones it documents raising -- degrades to a skip instead of
        # failing the step (and, unlike the route, a step failure would
        # otherwise cascade into FAILING THE WHOLE AGENT).
        #
        # The StepSkipped message itself is a fixed, generic one-liner --
        # never `str(exc)` -- because it lands verbatim on `AgentStep.error`,
        # which `GET /api/agents/{name}` serves to any viewer. A `set_avatar`
        # ClientError in particular can carry a full git/gh command line and
        # raw stderr; that belongs in the server log only, same sanitization
        # split the route this step mirrors already applies to its own
        # errors. The raw exception is logged here, server-side, before the
        # generic message is raised.
        try:
            png = clients.image.generate(prompt)
        except Exception as exc:  # degrade to skip, never fail the agent
            log.warning("avatar step %s for %s: %s", "generation", agent.name, exc)
            raise StepSkipped("picture generation failed") from exc

        # Size cap enforced BEFORE any store (repo push or DB cache), same
        # as the route.
        if len(png) > AVATAR_MAX_BYTES:
            raise StepSkipped(
                f"generated avatar exceeded the {AVATAR_MAX_BYTES}-byte cap ({len(png)} bytes)"
            )

        try:
            sha = set_avatar(clients.github, repo=agent.repo_full_name, png=png)
        except Exception as exc:  # push-side failure, same skip treatment
            log.warning("avatar step %s for %s: %s", "save", agent.name, exc)
            raise StepSkipped("couldn't save the picture") from exc

        # No explicit commit here: `_run_pipeline_body` (kobo_api.
        # provisioner, ~227-229) already commits the session right after
        # every step's `run` returns successfully, so this assignment rides
        # that same commit. A second commit here bought nothing on the
        # happy path and, on failure, poisoned the session for good --
        # the provisioner's own FAILED-branch commit (same function, the
        # `except Exception` arm right below the success path) would then
        # raise its own `PendingRollbackError` instead of ever recording
        # this step's honest `StepSkipped`, and the agent would die with a
        # raw SQLAlchemy error instead. Guarded the same way as every other
        # failure path in this method: a picture is never a reason to fail
        # the agent.
        try:
            agent.avatar_png = base64.b64encode(png).decode("ascii")
        except Exception as exc:
            ctx.session.rollback()
            log.warning("avatar step %s for %s: %s", "encode", agent.name, exc)
            raise StepSkipped("couldn't save the picture") from exc

        return f"generated and saved avatar ({sha})"


def _wait_active(ctx: Ctx, app_id: int) -> str:
    """Poll `ctx.clients.gojira.app_status(app_id)` every `POLL_INTERVAL_S` up
    to `POLL_TIMEOUT_S` until it reports `active`; raise on `failed` (naming
    the failed steps) or on exhausting every poll without either.

    Shared by OnboardStep (after its create_app/conflict resolution) and
    WireStep (after a reprovision triggered by a pushed contract change) —
    both wait on the exact same gojira state machine, just reached by a
    different trigger.
    """
    gojira = ctx.clients.gojira
    max_polls = POLL_TIMEOUT_S // POLL_INTERVAL_S
    status_payload: dict = {}
    for poll_count in range(max_polls):
        status_payload = gojira.app_status(app_id)
        status = status_payload.get("status")

        if status == "active":
            return f"gojira app {app_id} active"

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
            _sleep(POLL_INTERVAL_S)

    raise RuntimeError(
        f"timed out waiting for gojira app {app_id} to become active after "
        f"{POLL_TIMEOUT_S}s (last status: {status_payload.get('status')!r})"
    )


class OnboardStep:
    """Connects the pushed repo to gojira and waits for it to reach `active`."""

    name = "onboard"
    label = "Platform onboarding"

    def run(self, ctx: Ctx) -> str:
        agent = ctx.agent
        config = ctx.config
        gojira = ctx.clients.gojira

        try:
            app_id = gojira.create_app(
                name=agent.name,
                repo_full_name=agent.repo_full_name,
                cluster=config.gojira_cluster,
                namespace=agent.name,
            )
        except GojiraConflict as exc:
            if agent.gojira_app_id is None:
                # A conflict with nothing recorded means gojira already has
                # an app connected for this repo that kobo never created
                # (or never got far enough to record) — there's no id to
                # resume polling on, so this can't self-heal.
                raise RuntimeError(
                    f"gojira already has an app connected for "
                    f"{agent.repo_full_name!r}, but no app id is recorded for "
                    f"agent {agent.name!r} to resume from: {exc}"
                ) from exc
            # Idempotent resume: a prior attempt's create_app already
            # succeeded and its id was recorded (persisted alongside that
            # attempt's FAILED step row, since the engine commits every
            # dirty attribute on the agent when it records a step failure).
            # This retry's create_app call re-hits the same repo and gojira
            # reports the conflict; trust the id we already have and just
            # keep polling it.
            app_id = agent.gojira_app_id
            # Live-incident fix: a conflict-resume alone can't recover from a
            # platform-side provisioning failure (e.g. an environmental
            # kubectl outage during the original attempt). create_app's
            # conflict only says the app already exists — it says nothing
            # about whether gojira's own provisioning of it ever succeeded.
            # If the id we're resuming onto is stuck in gojira's terminal
            # `failed` state, just polling would read that same stale
            # failure straight back and raise immediately, and nothing would
            # ever tell gojira to actually retry — the product Retry could
            # never converge, even after an operator fixes the environment.
            # So: check gojira's live status here and, if it's failed,
            # re-kick it via reprovision() before waiting — the same call
            # WireStep already makes for its own reprovision trigger. Any
            # other status (provisioning/queued/active) means gojira is
            # already converging or done on its own; keep today's behavior
            # and just wait.
            if gojira.app_status(app_id).get("status") == "failed":
                gojira.reprovision(app_id)
                # Live-incident fix #2: reprovision() being *accepted* is not
                # the same thing as the platform's status row having been
                # *updated* yet — live, gojira started the new run ~2s later,
                # but this step's very next read landed in the window before
                # that run flipped the row, and saw the OLD run's terminal
                # `failed` (with its now-irrelevant failed-steps detail)
                # straight back, raising immediately even though the rerun
                # was genuinely underway. This is a plain read-after-write
                # race — the same class of lag BuildStep already learned to
                # absorb on CI-run reads (P1c's stale-rerun grace) — so the
                # lesson applies here too: a write that's just been accepted
                # deserves a bounded grace period before a read that
                # contradicts it is trusted. Poll here, locally, until the
                # status actually leaves "failed" (or the small dedicated
                # budget above runs out); only then hand off to
                # `_wait_active`, whose own failed-means-failed semantic is
                # correct for every other caller and must NOT be loosened to
                # accommodate this — the grace belongs only to the moment
                # right after a reprovision kick.
                for grace_poll in range(ONBOARD_REPROVISION_GRACE_POLLS):
                    status = gojira.app_status(app_id).get("status")
                    if status != "failed":
                        break
                    if grace_poll < ONBOARD_REPROVISION_GRACE_POLLS - 1:
                        _sleep(POLL_INTERVAL_S)
                else:
                    raise RuntimeError(
                        f"gojira app {app_id} is still 'failed' after "
                        f"reprovision() was accepted and "
                        f"{ONBOARD_REPROVISION_GRACE_POLLS} poll(s) of grace — "
                        "the platform did not start the requested rerun for "
                        f"app {app_id}"
                    )
        else:
            agent.gojira_app_id = app_id

        return _wait_active(ctx, app_id)


class AccessStep:
    """Grants the agent's GSA workload identity, model-group membership,
    and (resilience arc, Task 14) write access to its own agent-memory
    replica prefix when the room has one configured."""

    name = "access"
    label = "Model access"

    def run(self, ctx: Ctx) -> str:
        agent = ctx.agent
        config = ctx.config
        cloud = ctx.clients.cloud

        gsa_name = f"agent-{agent.name}"
        email = cloud.ensure_gsa(gsa_name)
        cloud.bind_workload_identity(email, namespace=agent.name, ksa=gsa_name)
        cloud.add_to_group(email, config.model_group)
        if config.memory_replica_bucket:
            # Agent memory replication (Task 14): write access to THIS
            # agent's own prefix and nothing wider -- the same per-agent
            # namespace (`{agent.name}/`) `kobo_api.composer._litestream_
            # config` targets, so the entrypoint's litestream process can
            # actually write what it's configured to replicate to, and
            # never anything outside its own agent's namespace.
            cloud.grant_bucket_prefix_write(
                email, config.memory_replica_bucket, f"{agent.name}/"
            )
        return f"granted model access to {email}"


# --- WireStep: app-authored manifests, the P0 kustomization-registration gotcha ----

# App-authored (i.e. kobo-authored, not gojira-generated) k8s manifests.
# Both carry the "App-authored (preserved by the generator)" header for the
# same reason the real agent-hello repo's serviceaccount.yaml does: it's the
# human-readable signal, on top of the kustomization registration itself,
# that gojira's generator will never overwrite or drop these two files.
SERVICEACCOUNT_YAML_TEMPLATE = """\
# App-authored (preserved by the generator): the agent's identity.
# Workload Identity binds this KSA to the agent's GSA, whose group
# membership grants model access.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: agent-{name}
  namespace: {name}
  annotations:
    iam.gke.io/gcp-service-account: {gsa_email}
"""

PVC_YAML_TEMPLATE = """\
# App-authored (preserved by the generator): the agent's memory volume.
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {{name: agent-{name}-techne, namespace: {name}}}
spec:
  accessModes: [ReadWriteOnce]
  resources: {{requests: {{storage: 1Gi}}}}
"""

# uid of the runtime image's `agent` user (docs/superpowers/plans/
# 2026-08-01-p0-foundations.md's Dockerfile creates it) — the PVC mount needs
# fsGroup set to this so the seeder can actually write techne.db under /techne.
TECHNE_FS_GROUP = 10001


def _register_resources(kustomization_path, resource_names: list[str]) -> None:
    """Append each of `resource_names` to `kustomization_path`'s `resources`
    list if not already present, then dump the whole file back.

    This is the one atomic step that makes an app-authored manifest actually
    take effect: gojira's generator *preserves* existing kustomization
    entries, it does not discover files on disk by scanning the directory —
    a manifest written without a matching entry here is silently never
    applied. Parses and re-dumps rather than regenerating the file outright,
    so any other entries (gojira's own generated resources, or a prior
    app-authored one) survive untouched. Idempotent: re-registering an
    already-present name is a no-op, so re-running this step never produces
    duplicate entries.
    """
    data = yaml.safe_load(kustomization_path.read_text()) or {}
    resources = data.get("resources", [])
    for resource_name in resource_names:
        if resource_name not in resources:
            resources.append(resource_name)
    data["resources"] = resources
    kustomization_path.write_text(yaml.safe_dump(data, sort_keys=False))


def _append_secrets_from(data: dict, entry: str) -> None:
    """Append `entry` to `data`'s `secrets_from` list if it isn't already
    there, never replacing whatever else is already in the list —
    `secrets_from` is a generator field the deploy platform reads to build
    the Deployment's `envFrom`.

    Generic over the entry name — unlike `kobo_api.slack_setup._update_
    gojira_secrets_from` (that module's own hardcoded-suffix sibling, for
    its own standalone clone/edit/push flow outside this file) — so this
    version can fold into the SAME `gojira.yaml` write `_update_gojira_yaml`
    below already makes, for the colleague-pass secret (Task 3/B3), rather
    than round-tripping the file a second time the way `wire_slack` does for
    its own `{name}-slack` entry.
    """
    secrets_from = data.get("secrets_from", [])
    if entry not in secrets_from:
        secrets_from.append(entry)
    data["secrets_from"] = secrets_from


def _update_gojira_yaml(gojira_yaml_path, agent, config) -> None:
    """Round-trip `gojira.yaml`, setting the service_account/env/data_mount
    keys the platform needs to wire the KSA, model env, and techne PVC into
    the generated Deployment. `yaml.safe_load`/`safe_dump` round-tripping
    (rather than preserving comments) is fine here: gojira regenerates the
    manifests from this file on every reprovision anyway, so this file's own
    comments aren't load-bearing the way kustomization.yaml's registered
    entries are.

    When the switchboard is configured (`cabal_enabled(config)`), also
    appends `{agent.name}-cabal` to `secrets_from` (Task 3/B3: every agent
    is born with a colleague pass) — folded into this SAME read-modify-write
    rather than a second round trip, so `WireStep` never needs a second
    reprovision beyond the one it already triggers for this file's other
    edits. Disabled, this function's behavior is byte-identical to before
    this feature existed.
    """
    data = yaml.safe_load(gojira_yaml_path.read_text()) or {}
    data["service_account"] = f"agent-{agent.name}"
    data["env"] = {
        "VERTEXAI_PROJECT": config.vertex_project,
        # Model-specific Vertex location (Gemini-3.1-Pro-Preview arc): this
        # is the env var the deployed pod's own LiteLLM client actually
        # reads, so it must resolve through the same vertex_location_for
        # lookup RepoStep's BundleSpec.vertex_location uses at compose time
        # — never config.vertex_location directly, or a global-only preview
        # would still 404 against the org's regional default at runtime.
        "VERTEXAI_LOCATION": vertex_location_for(config, agent.model_id),
    }
    data["data_mount"] = {
        "pvc": f"agent-{agent.name}-techne",
        "path": "/techne",
        "fs_group": TECHNE_FS_GROUP,
    }
    if cabal_enabled(config):
        _append_secrets_from(data, f"{agent.name}-cabal")
    gojira_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))


class WireStep:
    """Writes the agent's app-authored k8s manifests (KSA + techne PVC),
    registers both in kustomization.yaml, and edits gojira.yaml's contract
    (service_account/env/data_mount) — then pushes and, only if that push
    actually changed something, reprovisions and waits for gojira to land the
    change.

    This is the step that encodes the P0 finding described at module level:
    writing a manifest and registering it are one responsibility, never split
    — every test for this step checks kustomization.yaml's *resources* list,
    not just that the manifest files exist on disk.

    Invariant: this step never returns without confirming gojira reached
    `active` — including the no-change resume path (see `run`'s comment
    there), so a crash between a prior run's `reprovision()` and its
    `_wait_active` can never leave the step marked DONE against an
    unconfirmed app.

    When the switchboard is configured (`cabal_enabled(ctx.config)` AND
    `ctx.clients.cabal` is actually wired — the same two-gate discipline
    `HelloStep`/`DirectoryStep` already use), this step also delivers the
    agent's colleague pass (Task 3/B3): `_update_gojira_yaml` above folds
    the `{agent.name}-cabal` `secrets_from` entry into the SAME gojira.yaml
    write this step already makes (no second reprovision), and
    `kobo_api.colleagues.deliver_pass` mints and delivers the token BEFORE
    `commit_push` below — so by the time the push (and the reprovision it
    may trigger) lands, the Secret it names already exists in the
    namespace. Disabled, every part of this addition is a no-op and the
    step's behavior is byte-identical to before this feature existed.
    """

    name = "wire"
    label = "Deploy wiring"

    def run(self, ctx: Ctx) -> str:
        agent = ctx.agent
        config = ctx.config
        github = ctx.clients.github
        gojira = ctx.clients.gojira

        clone_dir = ctx.workdir / "wire"
        # Retry safety (mirrors RepoStep's compose_dir rmtree): a prior
        # attempt may have left a partial clone behind.
        shutil.rmtree(clone_dir, ignore_errors=True)
        github.clone(agent.repo_full_name, clone_dir)

        k8s_base = clone_dir / "k8s" / "base"
        # gsa_email() is prefix-agnostic — build the full "agent-<name>" id
        # here, same as AccessStep does for cloud.ensure_gsa().
        sa_email = gsa_email(f"agent-{agent.name}", config.gcp_project)
        (k8s_base / "serviceaccount.yaml").write_text(
            SERVICEACCOUNT_YAML_TEMPLATE.format(name=agent.name, gsa_email=sa_email)
        )
        (k8s_base / "pvc.yaml").write_text(PVC_YAML_TEMPLATE.format(name=agent.name))

        # The P0 gotcha, encoded: both new manifests MUST be registered here,
        # in the same step that writes them — never left for a later step or
        # made conditional.
        _register_resources(k8s_base / "kustomization.yaml", ["serviceaccount.yaml", "pvc.yaml"])

        _update_gojira_yaml(clone_dir / "gojira.yaml", agent, config)

        # Colleague pass delivery (Task 3/B3): BEFORE the push below, so the
        # Secret `{agent.name}-cabal`'s `secrets_from` entry (just written
        # into gojira.yaml above) already has something to reference by the
        # time that push lands and any reprovision it triggers regenerates
        # the Deployment's envFrom.
        if cabal_enabled(config) and ctx.clients.cabal is not None:
            deliver_pass(ctx.clients.kubectl, ctx.clients.cabal, agent)

        changed = github.commit_push(clone_dir, "kobo: wire service account, PVC, model env")
        if not changed:
            # Idempotent resume: an earlier attempt already pushed this exact
            # wiring (or nothing about the agent's contract has changed since
            # the last successful wire) — nothing for gojira to regenerate,
            # so don't reprovision for a change that isn't coming. But this
            # step must STILL confirm gojira is active before returning: a
            # prior attempt may have crashed after calling reprovision() but
            # before its own _wait_active completed, in which case gojira
            # could still be mid-provision (or have actually failed) even
            # though there's nothing left to push. Polling is cheap when
            # already active (the first poll returns immediately) and this
            # is the only way to close that crash window.
            _wait_active(ctx, agent.gojira_app_id)
            return "no wiring changes; app active"

        gojira.reprovision(agent.gojira_app_id)
        return _wait_active(ctx, agent.gojira_app_id)


# --- BuildStep ------------------------------------------------------------------


class BuildStep:
    """Polls the pushed repo's CI run for the repo's *current* head commit
    until it completes, self-healing one first-run failure per watched
    commit along the way; raises unless it eventually succeeds.

    Replaces an earlier version that watched `github.latest_run` (the most
    recent run anywhere in the repo) and could latch onto a stale run: a
    regeneration PR's own CI completing right after this step's real push
    could be mistaken for this push's result. Fixed by polling
    `runs_for_commit` against a specific sha rather than "latest across the
    whole repo" — a run for any other commit can never be mistaken for this
    one, structurally, no matter what else is running in the repo
    concurrently.

    Three more field-observed defects, all addressed here, are fixed together:

    1. `head_sha` is re-resolved on *every* poll, not once at entry. gojira's
       setup PR auto-merges onto this repo mid-provision, and kobo's own
       WireStep push races that merge landing — a single read-after-write
       can catch the head mid-transition and pin the rest of this
       invocation to a commit that's already been superseded. Nothing else
       pushes to these repos, so a resolved head that changes between polls
       means exactly one thing: the setup-PR merge just landed. When that
       happens, BuildStep switches to watching the new sha from that poll
       onward (noted in the detail string) rather than staying pinned to
       the stale one — the one-rerun heal budget below applies per watched
       sha, so an advance gets its own fresh budget (bounded: this
       pipeline pushes at most twice per provision, so at most one advance
       is ever expected).

    2. A completed run's *conclusion* is treated as heal-able whenever it
       isn't "success" — not only on an auth-propagation failure (kobo's
       first two live builds both failed on WIF auth propagation, the
       original motivation for the rerun), but also e.g. "cancelled": a live
       incident saw GitHub's concurrency pending-queue rule cancel the newer
       of two pending runs in a group outright rather than queueing it. The
       product's Retry button used to just re-poll that same stuck run
       forever, requiring an operator to `gh run rerun` by hand — now any
       completed non-success conclusion triggers exactly one automatic
       rerun per watched sha (so Retry is self-sufficient), and the eventual
       success detail names which conclusion it healed. A second non-success
       against the same watched sha in the same invocation is treated as a
       real failure and raises, naming the run id and conclusion.

    3. That second-failure check is itself given a short post-rerun grace
       (`BUILD_RERUN_STALE_GRACE_POLLS` polls) against gh's read-after-write
       lag: the poll right after `rerun()` fires can still come back with
       the *same* completed/non-success payload the rerun was meant to fix,
       simply because GitHub's API hasn't caught up with its own rerun yet.
       Without the grace window that stale echo would be misread as a
       genuine second failure and raise immediately, before the rerun ever
       had a chance to actually run.

    4. A cap on head advances (`BUILD_MAX_HEAD_ADVANCES`) prevents unbounded
       looping if the head oscillates between multiple shas mid-poll (a sign
       of unexpected concurrent pushes beyond the pipeline's pushes-at-most-
       twice invariant). On exceeding the cap, BuildStep raises rather than
       continuing to loop and potentially healing indefinitely.
    """

    name = "build"
    label = "Image build"

    def run(self, ctx: Ctx) -> str:
        agent = ctx.agent
        github = ctx.clients.github

        # Watched sha: re-resolved at the top of every iteration below
        # (never cached across polls) — `sha` starts as None purely so the
        # very first resolve is recognized as "establishing a baseline"
        # rather than "the head just advanced".
        sha: str | None = None
        rerun_attempted = False
        # The conclusion the one-per-watched-sha rerun healed, if any — named
        # in the eventual success detail rather than the old generic
        # "(after automatic rerun)" phrasing, since the fix that made any
        # non-success conclusion heal-able (not just "failure") makes naming
        # which one actually happened worth surfacing.
        healed_conclusion: str | None = None
        # Set once if/when the watched head advances mid-poll; carried
        # through to the final success detail regardless of which poll it
        # happened on, since it's a fact about this invocation as a whole.
        advance_note: str | None = None
        # Count the number of times the head advances (changes between polls);
        # when this exceeds BUILD_MAX_HEAD_ADVANCES, raise a bounded error
        # rather than looping indefinitely (see point 4 in the class docstring
        # and BUILD_MAX_HEAD_ADVANCES). Track observed shas to include them
        # in the error message.
        head_advances = 0
        observed_shas: list[str] = []
        # The run id the one rerun was fired against, and how many more
        # polls of that run get benefit-of-the-doubt on a stale
        # completed/non-success echo (see BUILD_RERUN_STALE_GRACE_POLLS and
        # point 3 in the class docstring). 0 means "no grace in effect" —
        # either no rerun has fired yet, or its grace window has lapsed.
        reran_run_id: int | None = None
        stale_grace_polls_remaining = 0

        max_polls = BUILD_POLL_TIMEOUT_S // BUILD_POLL_INTERVAL_S
        # A countdown rather than `for poll_count in range(max_polls)` so the
        # self-heal rerun below can reset it: a first build that fails late
        # (say, poll 55 of 60) would otherwise leave the healed rerun only
        # the few polls left in the *original* budget to complete in,
        # starving it of the time a fresh build actually needs. Bounded:
        # this reset can fire at most once per watched sha (gated by
        # `rerun_attempted`), so the worst case is two full budgets per sha
        # — never unbounded. Note this countdown is NOT reset merely because
        # the watched sha advances — only an actual rerun resets it — so the
        # overall timeout still bounds the invocation even across an advance.
        polls_remaining = max_polls
        runs: list[dict] = []
        while polls_remaining > 0:
            new_sha = github.head_sha(agent.repo_full_name)
            if sha is not None and new_sha != sha:
                head_advances += 1
                if head_advances > BUILD_MAX_HEAD_ADVANCES:
                    if sha not in observed_shas:
                        observed_shas.append(sha)
                    if new_sha not in observed_shas:
                        observed_shas.append(new_sha)
                    raise RuntimeError(
                        f"head advanced more than {BUILD_MAX_HEAD_ADVANCES} times — "
                        f"repository is receiving unexpected pushes (observed shas: "
                        f"{observed_shas!r})"
                    )
                if new_sha not in observed_shas:
                    observed_shas.append(new_sha)
                log.info(
                    "BuildStep head for %s advanced from %s to %s mid-poll; "
                    "switching to watch the new commit",
                    agent.repo_full_name, sha, new_sha,
                )
                advance_note = f"head advanced to {new_sha[:8]}"
                # A new commit gets its own one-rerun heal budget — the old
                # sha's rerun (if any, including any grace still in effect
                # for it) has no bearing on this one.
                rerun_attempted = False
                healed_conclusion = None
                reran_run_id = None
                stale_grace_polls_remaining = 0
            sha = new_sha

            runs = github.runs_for_commit(agent.repo_full_name, sha)
            polls_remaining -= 1

            # This poll gets the benefit of the doubt on a stale
            # completed/non-success echo if a grace window is still open —
            # consumed here (not only when it's actually used) so the
            # window is a bounded number of *polls* since the rerun, not a
            # bounded number of *stale sightings*.
            in_stale_grace = stale_grace_polls_remaining > 0
            if stale_grace_polls_remaining > 0:
                stale_grace_polls_remaining -= 1

            # Empty means CI hasn't registered a run against this commit
            # yet (registration lag right after the push) — keep polling,
            # this is not a failure.
            if runs:
                # Newest-first; a commit normally has exactly one CI run,
                # but if more than one somehow exists (e.g. a manual
                # workflow_dispatch alongside the push-triggered run) the
                # most recent is the relevant one.
                run = runs[0]
                if run.get("status") == "completed":
                    run_id = run.get("id")
                    conclusion = run.get("conclusion")
                    if conclusion == "success":
                        notes = []
                        if healed_conclusion is not None:
                            notes.append(f"healed a {healed_conclusion} run")
                        if advance_note is not None:
                            notes.append(advance_note)
                        suffix = f" ({'; '.join(notes)})" if notes else ""
                        return (
                            f"build run {run_id} succeeded for commit {sha}{suffix}"
                        )
                    if in_stale_grace and run_id == reran_run_id:
                        # Almost certainly gh's read-after-write lag echoing
                        # the pre-rerun payload back at us, not a genuine
                        # second failure — ignore and keep polling. See
                        # BUILD_RERUN_STALE_GRACE_POLLS / docstring point 3.
                        log.info(
                            "build run %s for commit %s still reports stale "
                            "pre-rerun conclusion %r after rerun; ignoring "
                            "(%d grace poll(s) left)",
                            run_id, sha, conclusion, stale_grace_polls_remaining,
                        )
                    elif not rerun_attempted:
                        rerun_attempted = True
                        healed_conclusion = conclusion
                        reran_run_id = run_id
                        log.info(
                            "build run %s for commit %s completed with conclusion "
                            "%r; auto-rerunning once",
                            run_id, sha, conclusion,
                        )
                        github.rerun(agent.repo_full_name, run_id)
                        # Keep polling the same sha/run — gh reruns reuse
                        # the run's id rather than minting a new one. Reset
                        # the budget: see the countdown comment above.
                        polls_remaining = max_polls
                        stale_grace_polls_remaining = BUILD_RERUN_STALE_GRACE_POLLS
                    else:
                        raise RuntimeError(
                            f"build run {run_id} completed with conclusion "
                            f"{conclusion!r} for commit {sha} (after an "
                            "automatic rerun already failed once this "
                            "invocation)"
                        )

            if polls_remaining > 0:
                _sleep(BUILD_POLL_INTERVAL_S)

        raise RuntimeError(
            f"timed out waiting for build run to complete after "
            f"{BUILD_POLL_TIMEOUT_S}s for commit {sha} (last runs: {runs!r})"
        )


# --- DeployStep -------------------------------------------------------------------


class DeployStep:
    """Waits for the generated Deployment's rollout to finish, then verifies
    the *live* spec actually carries WireStep's contract before declaring
    victory.

    Defense-in-depth for a field incident: a regeneration-PR's `kubectl
    apply` raced WireStep's own apply and landed last, leaving the live
    Deployment on an older, unwired tree — no model env, no KSA. `rollout_ok`
    alone couldn't catch this: the older tree rolled out perfectly cleanly,
    it just wasn't the generation the wire step intended. DeployStep passed,
    and the gap only surfaced two steps later as a confusing HelloStep
    failure. Task 1 (this plan) serializes the generated CI so the race
    itself shouldn't recur, but this check catches the *symptom* — a live
    spec missing the wired contract — structurally, regardless of root
    cause, right where the wiring is knowable and the remediation (retry
    once the latest run has landed) is obvious.
    """

    name = "deploy"
    label = "Deploy"

    def run(self, ctx: Ctx) -> str:
        agent = ctx.agent
        kubectl = ctx.clients.kubectl

        if not kubectl.rollout_ok(namespace=agent.name, deploy=agent.name):
            raise RuntimeError(f"rollout failed for {agent.name}/{agent.name}")

        env_names = kubectl.deploy_env_names(namespace=agent.name, deploy=agent.name)
        missing = [
            var for var in ("VERTEXAI_PROJECT", "VERTEXAI_LOCATION") if var not in env_names
        ]
        if missing:
            raise RuntimeError(
                f"live spec for {agent.name}/{agent.name} is missing wired model "
                f"env {missing!r} (live env names: {env_names!r}) — a concurrent "
                "CI apply may have raced the wire step's own apply and landed an "
                "older tree; retry after the latest run completes"
            )

        expected_sa = f"agent-{agent.name}"
        actual_sa = kubectl.deploy_service_account(namespace=agent.name, deploy=agent.name)
        if actual_sa != expected_sa:
            raise RuntimeError(
                f"live spec for {agent.name}/{agent.name} has service account "
                f"{actual_sa!r}, expected {expected_sa!r} — a concurrent CI apply "
                "may have raced the wire step's own apply and landed an older "
                "tree; retry after the latest run completes"
            )

        return f"rollout ok for {agent.name}; live spec verified"


# --- HelloStep --------------------------------------------------------------------


def _earn_capability(ctx: Ctx) -> None:
    """Generate this agent's capability sentence and push it to its card —
    the "capability is earned" moment (persona-metadata plan, Task 3),
    hooked onto HelloStep's tail, right after first contact succeeds.

    Global Constraints: capability generation must NEVER block readiness —
    every failure here (persona/skill/tooling lookups, the model call
    itself, the card commit, the directory resync below) is caught, logged,
    and swallowed; HelloStep's own reply is already recorded on `ctx.agent`
    by the time this runs and nothing this function does can undo that.

    Four independent boundaries, deliberately: generation (persona body +
    skill descriptions + tooling filenames -> one capability string) is one
    unit — any failure there leaves `agent.capability` untouched (null,
    unless a prior earn/PATCH/Regenerate already set it). Once generation
    succeeds, `agent.capability` is stored on the row immediately (the row
    is the room's own truth), and ONLY THEN is the roster/self.md refresh
    below fired, in its own boundary, before the card commit is even
    attempted: neither redelivery reads or waits on the GitHub card, so
    there is no reason to gate one on the other. The card commit is a
    further, independent boundary: a failure there leaves the just-stored
    capability in place but the card uncommitted (logged), since delivery
    is best-effort and a later Regenerate can always retry it. This
    ordering is why `tooling_filenames_for` swallows its own github-side
    failures internally (never propagating into the generation boundary);
    otherwise a broken GitHub client would prevent generation (and the row
    store) from ever happening at all, when the only thing it should
    actually block is the card commit.

    A fourth, best-effort tail (colleagues switchboard plan, Task 4b): only
    after that card commit actually SUCCEEDS, and only when the switchboard
    is configured, re-run `kobo_api.directory.sync_directory` so the
    directory's own copy of the card picks up the capability line
    `DirectoryStep` (which ran earlier in the pipeline now, before HelloStep)
    legitimately couldn't have seen yet. A raising cabal client here is
    caught and logged, same discipline as the card-commit boundary above —
    it must never fail HelloStep, and by construction can't touch
    `agent.capability` (already stored) or the just-committed card (already
    committed) either way.
    """
    agent = ctx.agent
    try:
        engine = ctx.session.get_bind()
        persona_body = _resolve_persona_body(ctx.config, ctx.clients.storage, engine, agent)
        skill_descriptions = skill_descriptions_for_agent(engine, agent)
        tooling_filenames = tooling_filenames_for(
            ctx.clients.github,
            ctx.clients.kubectl,
            ctx.clients.agent,
            repo=agent.repo_full_name,
            namespace=agent.name,
            deploy=agent.name,
        )
        capability = generate_capability(
            ctx.clients.name, persona_body, skill_descriptions, tooling_filenames
        )
    except Exception:
        log.warning(
            "capability generation failed for agent %s — leaving capability unset",
            agent.name,
            exc_info=True,
        )
        return

    agent.capability = capability

    try:
        # Roster refresh lane (Task 6): the room's regenerated view of this
        # agent (its colleagues.md line, and its own self.md) reaches
        # everyone affected the moment capability distills -- own boundary,
        # ahead of the card commit below, since neither redelivery touches
        # GitHub at all. Best-effort and switchboard-gated internally;
        # this outer try/except is belt-and-suspenders against a coding
        # bug in the refresh path itself, matching every other boundary
        # here (capability generation must never block readiness).
        refresh_after_capability_distill(ctx.clients, ctx.config, ctx.session, agent)
    except Exception:
        log.warning(
            "roster/self-knowledge refresh failed for agent %s after capability "
            "distilled; affected rosters and this agent's self.md may be stale "
            "until the next change",
            agent.name,
            exc_info=True,
        )

    try:
        commit_card_metadata(
            ctx.clients.github,
            agent.repo_full_name,
            team=agent.team,
            purpose_tags=decode_tags(agent.purpose_tags),
            capability=capability,
        )
    except ClientError:
        log.warning(
            "card commit failed for agent %s — capability stored, card left "
            "uncommitted (Regenerate can retry)",
            agent.name,
            exc_info=True,
        )
    else:
        if cabal_enabled(ctx.config):
            try:
                sync_directory(ctx.clients.github, ctx.clients.cabal, ctx.config, agent)
            except Exception:
                log.warning(
                    "directory resync failed for agent %s after the capability "
                    "card commit — the directory's copy stays without the "
                    "capability line until the next successful resync",
                    agent.name,
                    exc_info=True,
                )


def _make_first_contact(ctx: Ctx, hello_text: str) -> str:
    """One attempt at first contact, via whichever path is selected:
    directly over the agent's a2a endpoint (`ctx.clients.agent.hello`, the
    legacy path) or, when the switchboard is wired, by relaying the same
    a2a task envelope through it (`ctx.clients.cabal.relay`).

    Contact path selection (colleagues switchboard plan, Task 4b): when
    `cabal_enabled(ctx.config)` is true AND `ctx.clients.cabal` is actually
    wired (either gate alone isn't enough, same two-gate discipline the
    chat route already uses), the relay path builds the exact same task
    envelope shape `_build_task_envelope` produces (never a forked shape)
    and reads the reply back through `_extract_relay_reply`, the identical
    helper the chat route uses (`kobo_api.clients`), so the two callers can
    never drift on how a relayed reply is read. When the switchboard isn't
    wired, the direct path runs completely unchanged: byte-identical to
    before Task 4b, Global Constraints' "org keys optional = feature off =
    legacy behavior".

    Raises `ClientError` (a `CabalError` from `relay` itself or from
    `_extract_relay_reply` on a malformed reply is a subclass) on failure;
    `HelloStep.run` is this function's only caller and is what retries it.
    """
    agent = ctx.agent
    if cabal_enabled(ctx.config) and ctx.clients.cabal is not None:
        envelope = _build_task_envelope([{"role": "user", "text": hello_text}])
        raw = ctx.clients.cabal.relay(agent.name, envelope)
        return _extract_relay_reply(raw)
    return ctx.clients.agent.hello(namespace=agent.name, service=agent.name, text=hello_text)


class HelloStep:
    """First contact: says hello to the freshly-deployed agent, the "one-
    click" pipeline's last step. Contact itself is one attempt of
    `_make_first_contact` (see there for the direct-vs-relay path
    selection); this class's own job is retrying that attempt and, once it
    succeeds, running the capability tail.

    Retry (live incident 2026-08-27): a brand-new agent's minutes-old
    service account and workload-identity binding can still be propagating
    when first contact is made, so a `ClientError`/`CabalError` here, on
    either path, gets up to `HELLO_RETRY_ATTEMPTS` tries, `HELLO_RETRY_
    BACKOFF_S` apart (the module's own `_sleep` seam, never a real sleep in
    a test), before it's left to propagate up to the engine's own per-step
    try/except: failing this step exactly the way an uncaught `ClientError`
    from `hello()` always has, same retryable semantics, just absorbing a
    known transient window first instead of making the operator click Retry
    for it by hand. This ordering only works because `DirectoryStep` now
    runs BEFORE this step (Task 4b's reorder): the agent is already
    registered in the switchboard's directory by the time first contact
    needs to ride its relay.

    Its tail (`_earn_capability`, persona-metadata plan Task 3, extended by
    Task 4b's directory resync) generates this agent's capability sentence
    and pushes it to its card. Wrapped in its own defensive try/except here
    too (belt-and-suspenders on top of `_earn_capability`'s own internal
    boundaries) so that even a bug in that code path could never turn a
    successful first contact into a failed HelloStep: capability
    generation must never block readiness.
    """

    name = "hello"
    label = "First contact"

    def run(self, ctx: Ctx) -> str:
        agent = ctx.agent
        hello_text = "hello! how did you get here?"

        reply: str | None = None
        for attempt in range(1, HELLO_RETRY_ATTEMPTS + 1):
            try:
                reply = _make_first_contact(ctx, hello_text)
                break
            except ClientError:
                if attempt == HELLO_RETRY_ATTEMPTS:
                    raise
                log.warning(
                    "hello attempt %d/%d failed for agent %s, retrying in "
                    "%ds (fresh-identity propagation window)",
                    attempt,
                    HELLO_RETRY_ATTEMPTS,
                    agent.name,
                    HELLO_RETRY_BACKOFF_S,
                    exc_info=True,
                )
                _sleep(HELLO_RETRY_BACKOFF_S)
        agent.first_reply = reply

        try:
            _earn_capability(ctx)
        except Exception:
            log.warning(
                "capability generation/card commit crashed unexpectedly for "
                "agent %s",
                agent.name,
                exc_info=True,
            )

        return f"agent replied: {reply!r}"


# --- DirectoryStep (colleagues switchboard plan, Task 2: the directory seam) ------


class DirectoryStep:
    """Registers the freshly-deployed agent in the switchboard's directory
    (`kobo_api.directory.sync_directory`) — runs right BEFORE `HelloStep`
    (colleagues switchboard plan, Task 4b's reorder: agents compose LOCKED,
    only switchboard-signed calls accepted, so first contact needs the
    agent already registered before it can ride the relay). The card
    registered here legitimately lacks a capability line yet — that's
    earned moments later, on HelloStep's own tail, whose best-effort
    resync backfills the directory's copy once it exists; this step makes
    no attempt to generate or wait for one itself.

    Global Constraints: org config is boot-time only, and an absent
    switchboard means the feature is off everywhere — `cabal_enabled` being
    false makes this step a plain no-op that returns normally (DONE), so a
    room with no switchboard configured stays exactly as green on this step
    as it was before the step existed. When the switchboard IS configured,
    any `sync_directory` failure (a card-fetch problem, a non-2xx
    `graduate`) is left to propagate — the engine's own per-step try/except
    (`kobo_api.provisioner._run_pipeline_body`) turns that into a FAILED row
    the product's existing Retry already knows how to re-run, same as every
    other step here. Either way this step never blocks or reruns anything
    earlier in the chain — its own outcome is the only thing it can affect.
    """

    name = "directory"
    label = "Joins the agent directory"

    def run(self, ctx: Ctx) -> str:
        if not cabal_enabled(ctx.config):
            return "switchboard not configured; nothing to register"

        sync_directory(ctx.clients.github, ctx.clients.cabal, ctx.config, ctx.agent)
        return f"registered {ctx.agent.name} in the directory"
