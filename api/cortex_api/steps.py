"""Provisioning steps: repo, onboard, access, wire, build, deploy.

Each class satisfies `cortex_api.provisioner.Step` (a `name` attribute plus
`run(ctx) -> str`) and is meant to run inside `run_pipeline`'s per-step
try/except boundary: raising from `run()` fails just that step (and aborts
everything after it), returning hands the pipeline a human-readable detail
string that isn't persisted anywhere today.

Resume story for both external-creation steps (repo, onboard) mirrors the
same shape: re-check the outside world's state rather than trusting only
what's in the DB row, and only treat a pre-existing thing as *this* agent's
prior work when the row already recorded it — otherwise raise, since a
same-named repo/app that cortex didn't create is not safe to adopt silently.

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

import logging
import shutil
import time
from dataclasses import replace
from typing import TYPE_CHECKING

import yaml

from cortex_api.catalog import list_personas, list_skills
from cortex_api.clients import GojiraConflict, gsa_email
from cortex_api.composer import BundleSpec, compose, validate_agent_yaml
from cortex_api.config import Config
from cortex_api.models import Agent

log = logging.getLogger(__name__)

# Type-only: `cortex_api.provisioner` imports this module (for `PIPELINE`), so a
# runtime import here would be circular. `from __future__ import annotations`
# (above) already makes every annotation in this file a lazy string, so `Ctx`
# is only ever needed by type checkers, never at import time.
if TYPE_CHECKING:
    from cortex_api.provisioner import Ctx

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

# Module-level sleep seam: tests monkeypatch this to a no-op so a 300s/5s (or
# 600s/10s) poll loop — or a deliberately-triggered timeout, which walks the
# whole loop — never actually blocks. Referenced as a bare name inside each
# step's run() so the monkeypatch (which rebinds this module attribute) is
# visible at call time.
_sleep = time.sleep


def _resolve_persona_body(config: Config, agent: Agent) -> str:
    """Look up `agent.persona` in the persona catalog and `{name}`-format its body."""
    personas = {p.id: p for p in list_personas(config.personas_dir)}
    persona_info = personas.get(agent.persona)
    if persona_info is None:
        raise RuntimeError(
            f"persona {agent.persona!r} (recorded on agent {agent.name!r}) is not "
            "in the persona catalog"
        )
    return persona_info.body.format(name=agent.name)


def _resolve_skills(config: Config, agent: Agent) -> list:
    """Resolve `agent.skills` (a list of `{"name": ..., "sha": ...}` dicts, the
    shape the DB column holds) against the current skill catalog by id.

    A skill id the agent recorded that's no longer in the catalog (removed
    upstream since the agent picked it) is a hard failure, not a silent drop:
    the composed bundle would otherwise ship without a skill the operator
    believes it has.

    Pinning (Task 6): the catalog lookup is by id only — its SkillInfo always
    carries the skills repo's *current* HEAD sha (`list_skills`'s scan). The
    row's own recorded `sha` (set once, at first successful resolution — see
    `cortex_api.main`'s create-agent route) is authoritative over that and is
    substituted in here on every resolution, first-time or retry alike: a
    retry after the skills repo's HEAD has since moved must still compose the
    content from the sha this agent actually picked, not whatever HEAD is now.
    `compose()` (cortex_api.composer) is what actually enforces this pin, by
    materializing each skill via `git archive` at the sha it's handed here
    rather than copying the working tree.
    """
    catalog = {skill.id: skill for skill in list_skills(config.skills_repo_path)}
    resolved = []
    for entry in agent.skills:
        skill_id = entry["name"]
        skill_info = catalog.get(skill_id)
        if skill_info is None:
            raise RuntimeError(
                f"skill {skill_id!r} recorded on agent {agent.name!r} is no longer "
                "in the skill catalog"
            )
        recorded_sha = entry.get("sha")
        if recorded_sha:
            skill_info = replace(skill_info, sha=recorded_sha)
        resolved.append(skill_info)
    return resolved


class RepoStep:
    """Composes the agent's daedalus bundle and pushes it as a new GitHub repo."""

    name = "repo"
    label = "Repository"

    def run(self, ctx: Ctx) -> str:
        agent = ctx.agent
        config = ctx.config
        github = ctx.clients.github

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

        persona_body = _resolve_persona_body(config, agent)
        skills = _resolve_skills(config, agent)

        spec = BundleSpec(
            name=agent.name,
            persona_body=persona_body,
            model_id=agent.model_id,
            skills=skills,
            runtime_image=config.runtime_image,
            vertex_project=config.vertex_project,
            vertex_location=config.vertex_location,
        )

        compose_dir = ctx.workdir / "repo"
        # Retry safety: compose() raises FileExistsError if dest already has
        # the skills subtree (a prior attempt may have composed but failed
        # before/at create_repo_from_dir) — clear it before recomposing.
        shutil.rmtree(compose_dir, ignore_errors=True)
        compose(spec, compose_dir)
        validate_agent_yaml(compose_dir / "bundle")

        github.create_repo_from_dir(repo, compose_dir)
        agent.repo_full_name = repo
        return f"created {repo}"


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
                # an app connected for this repo that cortex never created
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
        else:
            agent.gojira_app_id = app_id

        return _wait_active(ctx, app_id)


class AccessStep:
    """Grants the agent's GSA workload identity and model-group membership."""

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
        return f"granted model access to {email}"


# --- WireStep: app-authored manifests, the P0 kustomization-registration gotcha ----

# App-authored (i.e. cortex-authored, not gojira-generated) k8s manifests.
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


def _update_gojira_yaml(gojira_yaml_path, agent, config) -> None:
    """Round-trip `gojira.yaml`, setting the service_account/env/data_mount
    keys the platform needs to wire the KSA, model env, and techne PVC into
    the generated Deployment. `yaml.safe_load`/`safe_dump` round-tripping
    (rather than preserving comments) is fine here: gojira regenerates the
    manifests from this file on every reprovision anyway, so this file's own
    comments aren't load-bearing the way kustomization.yaml's registered
    entries are.
    """
    data = yaml.safe_load(gojira_yaml_path.read_text()) or {}
    data["service_account"] = f"agent-{agent.name}"
    data["env"] = {
        "VERTEXAI_PROJECT": config.vertex_project,
        "VERTEXAI_LOCATION": config.vertex_location,
    }
    data["data_mount"] = {
        "pvc": f"agent-{agent.name}-techne",
        "path": "/techne",
        "fs_group": TECHNE_FS_GROUP,
    }
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

        changed = github.commit_push(clone_dir, "cortex: wire service account, PVC, model env")
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
       setup PR auto-merges onto this repo mid-provision, and cortex's own
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
       isn't "success" — not only on an auth-propagation failure (cortex's
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


class HelloStep:
    """First contact: says hello to the freshly-deployed agent over its a2a
    endpoint (via `ctx.clients.agent.hello`) and records its reply on the
    agent row so the UI can show it — the "one-click" pipeline's last step."""

    name = "hello"
    label = "First contact"

    def run(self, ctx: Ctx) -> str:
        agent = ctx.agent
        reply = ctx.clients.agent.hello(
            namespace=agent.name, service=agent.name, text="hello! how did you get here?"
        )
        agent.first_reply = reply
        return f"agent replied: {reply!r}"
