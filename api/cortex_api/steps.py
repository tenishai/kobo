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
"""

from __future__ import annotations

import shutil
import time

import yaml

from cortex_api.catalog import list_personas, list_skills
from cortex_api.clients import GojiraConflict
from cortex_api.composer import BundleSpec, compose, validate_agent_yaml
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

# Module-level sleep seam: tests monkeypatch this to a no-op so a 300s/5s (or
# 600s/10s) poll loop — or a deliberately-triggered timeout, which walks the
# whole loop — never actually blocks. Referenced as a bare name inside each
# step's run() so the monkeypatch (which rebinds this module attribute) is
# visible at call time.
_sleep = time.sleep


def _resolve_persona_body(config, agent) -> str:
    """Look up `agent.persona` in the persona catalog and `{name}`-format its body."""
    personas = {p.id: p for p in list_personas(config.personas_dir)}
    persona_info = personas.get(agent.persona)
    if persona_info is None:
        raise RuntimeError(
            f"persona {agent.persona!r} (recorded on agent {agent.name!r}) is not "
            "in the persona catalog"
        )
    return persona_info.body.format(name=agent.name)


def _resolve_skills(config, agent) -> list:
    """Resolve `agent.skills` (a list of `{"name": ..., "sha": ...}` dicts, the
    shape the DB column holds) against the current skill catalog by id.

    A skill id the agent recorded that's no longer in the catalog (removed
    upstream since the agent picked it) is a hard failure, not a silent drop:
    the composed bundle would otherwise ship without a skill the operator
    believes it has.
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
        gsa_email = f"agent-{agent.name}@{config.gcp_project}.iam.gserviceaccount.com"
        (k8s_base / "serviceaccount.yaml").write_text(
            SERVICEACCOUNT_YAML_TEMPLATE.format(name=agent.name, gsa_email=gsa_email)
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
            # so don't reprovision or poll for a change that isn't coming.
            return "no wiring changes to push"

        gojira.reprovision(agent.gojira_app_id)
        return _wait_active(ctx, agent.gojira_app_id)


# --- BuildStep ------------------------------------------------------------------


class BuildStep:
    """Polls the pushed repo's CI run until it completes; raises unless it
    succeeded."""

    name = "build"
    label = "Image build"

    def run(self, ctx: Ctx) -> str:
        agent = ctx.agent
        github = ctx.clients.github

        max_polls = BUILD_POLL_TIMEOUT_S // BUILD_POLL_INTERVAL_S
        run_payload: dict = {}
        for poll_count in range(max_polls):
            run_payload = github.latest_run(agent.repo_full_name)

            if run_payload.get("status") == "completed":
                run_id = run_payload.get("id")
                conclusion = run_payload.get("conclusion")
                if conclusion == "success":
                    return f"build run {run_id} succeeded"
                raise RuntimeError(
                    f"build run {run_id} completed with conclusion {conclusion!r}"
                )

            if poll_count < max_polls - 1:
                _sleep(BUILD_POLL_INTERVAL_S)

        raise RuntimeError(
            f"timed out waiting for build run to complete after "
            f"{BUILD_POLL_TIMEOUT_S}s (last status: {run_payload.get('status')!r})"
        )


# --- DeployStep -------------------------------------------------------------------


class DeployStep:
    """Waits for the generated Deployment's rollout to finish."""

    name = "deploy"
    label = "Deploy"

    def run(self, ctx: Ctx) -> str:
        agent = ctx.agent
        kubectl = ctx.clients.kubectl

        if not kubectl.rollout_ok(namespace=agent.name, deploy=agent.name):
            raise RuntimeError(f"rollout failed for {agent.name}/{agent.name}")
        return f"rollout ok for {agent.name}"
