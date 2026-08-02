"""The first three provisioning steps: repo, onboard, access.

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
"""

from __future__ import annotations

import shutil
import time

from cortex_api.catalog import list_personas, list_skills
from cortex_api.clients import GojiraConflict
from cortex_api.composer import BundleSpec, compose, validate_agent_yaml
from cortex_api.provisioner import Ctx

# Gojira onboarding poll cadence: every 5s, up to 300s total (the brief's numbers,
# as module constants so both the step and its tests share one source of truth).
POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 300

# Module-level sleep seam: tests monkeypatch this to a no-op so a 300s/5s poll
# loop (or a deliberately-triggered timeout, which walks the whole loop) never
# actually blocks. Referenced as a bare name inside OnboardStep.run() so the
# monkeypatch (which rebinds this module attribute) is visible at call time.
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

        max_polls = POLL_TIMEOUT_S // POLL_INTERVAL_S
        status_payload: dict = {}
        for _ in range(max_polls):
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

            _sleep(POLL_INTERVAL_S)

        raise RuntimeError(
            f"timed out waiting for gojira app {app_id} to become active after "
            f"{POLL_TIMEOUT_S}s (last status: {status_payload.get('status')!r})"
        )


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
