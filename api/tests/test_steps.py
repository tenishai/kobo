"""Tests for pnyx_api.steps: RepoStep, OnboardStep, AccessStep.

Each step is exercised directly — construct a `Ctx`, call `step.run(ctx)` — against
the platform-client fakes in tests/fakes.py, the same style test_engine.py uses for
its toy steps but with the real Task 6 steps. No real subprocess/network/sleep: the
module's `_sleep` is monkeypatched to a no-op everywhere a poll loop might run more
than a couple of iterations, so the whole file runs in well under a second.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pnyx_api.clients import Clients
from pnyx_api.composer import BundleSpec
from pnyx_api.config import Config
from pnyx_api.models import Agent
from pnyx_api.provisioner import Ctx
from pnyx_api.steps import (
    POLL_INTERVAL_S,
    POLL_TIMEOUT_S,
    AccessStep,
    OnboardStep,
    RepoStep,
)
from tests.fakes import FakeCloud, FakeGitHub, FakeGojira

AGENT_NAME = "clio"
PERSONA = "helpful-generalist"
MODEL_ID = "vertex_ai/claude-sonnet-4-5"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Every OnboardStep test runs a poll loop; never let it sleep for real."""
    import pnyx_api.steps as steps_module

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    fake_sleep.calls = sleep_calls
    monkeypatch.setattr(steps_module, "_sleep", fake_sleep)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _make_skills_repo(tmp_path: Path, skill_id: str = "echo") -> Path:
    repo = tmp_path / "skills-repo"
    skill_dir = repo / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: Echoes things.\n---\nEcho skill body.\n"
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _head_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_agent(
    session_factory,
    *,
    name: str = AGENT_NAME,
    persona: str = PERSONA,
    model_id: str = MODEL_ID,
    skills: list | None = None,
    repo_full_name: str | None = None,
    gojira_app_id: int | None = None,
):
    session = session_factory()
    agent = Agent(
        name=name,
        persona=persona,
        model_id=model_id,
        skills=skills or [],
        repo_full_name=repo_full_name,
        gojira_app_id=gojira_app_id,
    )
    session.add(agent)
    session.commit()
    return session, agent


def _make_ctx(session, agent, config, clients) -> Ctx:
    workdir = config.workdir / agent.name
    workdir.mkdir(parents=True, exist_ok=True)
    return Ctx(agent=agent, session=session, config=config, clients=clients, workdir=workdir)


def _clients(*, github=None, gojira=None, cloud=None) -> Clients:
    return Clients(github=github, gojira=gojira, cloud=cloud, agent=None, kubectl=None)


# --- RepoStep -----------------------------------------------------------------


def test_repo_step_happy_path_creates_and_records_repo(session_factory, config):
    session, agent = _make_agent(session_factory)
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    RepoStep().run(ctx)

    expected_repo = f"{config.github_org}/agent-{AGENT_NAME}"
    assert agent.repo_full_name == expected_repo
    assert github.calls == [f"repo_exists:{expected_repo}", f"create_repo_from_dir:{expected_repo}"]
    assert len(github.created_from_dir) == 1
    created_repo, created_src = github.created_from_dir[0]
    assert created_repo == expected_repo
    assert (created_src / "bundle" / "agent.yaml").is_file()


def test_repo_step_substitutes_persona_name_placeholder(session_factory, config):
    session, agent = _make_agent(session_factory, name="sam")
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    RepoStep().run(ctx)

    persona_text = (ctx.workdir / "repo" / "bundle" / "persona.md").read_text()
    assert "sam" in persona_text
    assert "{name}" not in persona_text


def test_repo_step_resolves_skills_by_id_into_bundle(session_factory, tmp_path):
    skills_repo = _make_skills_repo(tmp_path)
    # A real recorded sha (the fixture repo's actual HEAD) — this repo is a
    # real git repo, so RepoStep's skill materialization is now the
    # sha-pinned git-archive path (see pnyx_api.composer), which needs a
    # sha that's actually reachable.
    sha = _head_sha(skills_repo)
    config = Config(workdir=tmp_path / "work", skills_repo_path=skills_repo)
    session, agent = _make_agent(
        session_factory, skills=[{"name": "echo", "sha": sha}]
    )
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    RepoStep().run(ctx)

    assert (ctx.workdir / "repo" / "bundle" / "skills" / "echo" / "SKILL.md").is_file()


def test_repo_step_pins_skill_to_recorded_sha_even_after_head_advances(session_factory, tmp_path):
    """The Task 6 regression this whole change exists for: a retry (or any
    later recomposition) that resolves the agent row's recorded skill sha
    must ship the content from THAT sha — not whatever the skills repo's
    HEAD has since moved to. Before this fix, RepoStep re-resolved skills
    against the skills repo's current working tree on every run, silently
    shipping newer content once HEAD moved.
    """
    skills_repo = _make_skills_repo(tmp_path)
    old_sha = _head_sha(skills_repo)

    # Advance HEAD with changed skill content, as if someone pushed an update
    # to the skills repo after this agent picked "echo".
    (skills_repo / "skills" / "echo" / "SKILL.md").write_text(
        "---\nname: echo\ndescription: Echoes things.\n---\nNEW echo skill body.\n"
    )
    _git(skills_repo, "add", "-A")
    _git(skills_repo, "commit", "-q", "-m", "update echo")
    assert _head_sha(skills_repo) != old_sha

    config = Config(workdir=tmp_path / "work", skills_repo_path=skills_repo)
    session, agent = _make_agent(
        session_factory, skills=[{"name": "echo", "sha": old_sha}]
    )
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    RepoStep().run(ctx)

    shipped = (
        ctx.workdir / "repo" / "bundle" / "skills" / "echo" / "SKILL.md"
    ).read_text()
    assert "Echo skill body" in shipped
    assert "NEW echo skill body" not in shipped


def test_repo_step_unknown_skill_id_raises_clear_message(session_factory, config):
    session, agent = _make_agent(
        session_factory, skills=[{"name": "ghost-skill", "sha": "whatever"}]
    )
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError, match="ghost-skill"):
        RepoStep().run(ctx)

    # got as far as the repo_exists check, but never created anything
    assert github.created_from_dir == []


def test_repo_step_unknown_persona_id_raises_clear_message(session_factory, config):
    session, agent = _make_agent(session_factory, persona="ghost-persona")
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError, match="ghost-persona"):
        RepoStep().run(ctx)

    # got as far as the repo_exists check, but never created anything
    assert github.created_from_dir == []


def test_repo_step_existing_repo_not_recorded_raises(session_factory, config):
    expected_repo = f"{config.github_org}/agent-{AGENT_NAME}"
    session, agent = _make_agent(session_factory, repo_full_name=None)
    github = FakeGitHub(existing_repos={expected_repo})
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    with pytest.raises(RuntimeError):
        RepoStep().run(ctx)

    assert github.created_from_dir == []


def test_repo_step_resume_when_row_already_matches_skips_creation(session_factory, config):
    expected_repo = f"{config.github_org}/agent-{AGENT_NAME}"
    session, agent = _make_agent(session_factory, repo_full_name=expected_repo)
    github = FakeGitHub(existing_repos={expected_repo})
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    detail = RepoStep().run(ctx)

    assert "already" in detail
    # idempotent resume: no compose, no create_repo_from_dir call
    assert github.calls == [f"repo_exists:{expected_repo}"]
    assert github.created_from_dir == []
    assert not (ctx.workdir / "repo").exists()


def test_repo_step_rmtrees_stale_compose_dir_before_recomposing(session_factory, tmp_path):
    """Retry-safety: a prior partial attempt may have left a compose dir with a
    skills subtree behind; compose() raises FileExistsError on top of that, so
    RepoStep must clear it first."""
    skills_repo = _make_skills_repo(tmp_path, skill_id="echo")
    sha = _head_sha(skills_repo)
    config = Config(workdir=tmp_path / "work", skills_repo_path=skills_repo)
    session, agent = _make_agent(session_factory, skills=[{"name": "echo", "sha": sha}])
    github = FakeGitHub()
    ctx = _make_ctx(session, agent, config, _clients(github=github))

    # Pre-seed a stale skills subtree that compose() would refuse to overwrite
    stale_dir = ctx.workdir / "repo" / "bundle" / "skills" / "echo"
    stale_dir.mkdir(parents=True)
    (stale_dir / "stale_file.txt").write_text("leftover from failed attempt\n")

    # RepoStep should succeed despite the stale tree (rmtree clears it first)
    RepoStep().run(ctx)

    # Verify the stale file is gone and the fresh compose succeeded
    assert not (ctx.workdir / "repo" / "bundle" / "skills" / "echo" / "stale_file.txt").exists()
    assert (ctx.workdir / "repo" / "bundle" / "skills" / "echo" / "SKILL.md").is_file()
    assert (ctx.workdir / "repo" / "bundle" / "agent.yaml").is_file()


def test_repo_step_compose_raises_without_rmtree_on_stale_skills(tmp_path):
    """Sanity guard: verify that compose() would raise FileExistsError if we
    didn't rmtree the stale skills subtree first."""
    from pnyx_api.composer import compose

    skills_repo = _make_skills_repo(tmp_path, skill_id="echo")
    config = Config(workdir=tmp_path / "work", skills_repo_path=skills_repo)

    # Get the skill info for the spec
    from pnyx_api.catalog import list_skills
    skills = list_skills(skills_repo)

    spec = BundleSpec(
        name="test-agent",
        persona_body="You are {name}.",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=skills,
        runtime_image="image:latest",
        vertex_project="proj",
        vertex_location="us-central1",
    )

    compose_dir = config.workdir / "repo"

    # First compose succeeds
    compose(spec, compose_dir)

    # Second compose to the same location should raise FileExistsError (without rmtree)
    with pytest.raises(FileExistsError):
        compose(spec, compose_dir)


# --- OnboardStep ----------------------------------------------------------------


def _repo_full_name(config) -> str:
    return f"{config.github_org}/agent-{AGENT_NAME}"


def test_onboard_step_happy_path_sets_app_id_and_polls_to_active(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    gojira = FakeGojira(activate_after=3)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    OnboardStep().run(ctx)

    assert agent.gojira_app_id == 1
    assert gojira.calls[0] == f"create_app:{repo}"
    assert gojira.calls.count("app_status:1") == 3


def test_onboard_step_create_app_receives_expected_arguments(session_factory, config, monkeypatch):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    gojira = FakeGojira(activate_after=1)

    seen = {}
    real_create_app = gojira.create_app

    def _spy_create_app(*, name, repo_full_name, cluster, namespace):
        seen.update(
            name=name, repo_full_name=repo_full_name, cluster=cluster, namespace=namespace
        )
        return real_create_app(
            name=name, repo_full_name=repo_full_name, cluster=cluster, namespace=namespace
        )

    monkeypatch.setattr(gojira, "create_app", _spy_create_app)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    OnboardStep().run(ctx)

    assert seen == {
        "name": AGENT_NAME,
        "repo_full_name": repo,
        "cluster": config.gojira_cluster,
        "namespace": AGENT_NAME,
    }


def test_onboard_step_raises_with_failed_step_names(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    gojira = FakeGojira(
        activate_after=1,
        end_status="failed",
        failed_steps=[
            {"name": "clone", "status": "done"},
            {"name": "provision", "status": "failed", "error": "boom"},
        ],
    )
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    with pytest.raises(RuntimeError) as exc_info:
        OnboardStep().run(ctx)

    message = str(exc_info.value)
    assert "provision" in message
    assert "clone" not in message


def test_onboard_step_conflict_without_recorded_id_raises(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=None)
    gojira = FakeGojira(conflict_repos={repo})
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    with pytest.raises(RuntimeError):
        OnboardStep().run(ctx)

    assert agent.gojira_app_id is None


def test_onboard_step_resumes_on_conflict_when_id_already_recorded(session_factory, config):
    repo = _repo_full_name(config)
    gojira = FakeGojira(activate_after=1)
    # Seed the fake as if a prior attempt of this step already created the app
    # (this is what "a matching recorded id" means: the earlier attempt's
    # create_app succeeded and its id made it into agent.gojira_app_id via the
    # engine's commit on that earlier failed/aborted run).
    prior_app_id = gojira.create_app(
        name=AGENT_NAME, repo_full_name=repo, cluster=config.gojira_cluster, namespace=AGENT_NAME
    )
    gojira._conflict_repos.add(repo)  # now gojira reports a live conflict for this repo

    session, agent = _make_agent(session_factory, repo_full_name=repo, gojira_app_id=prior_app_id)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    OnboardStep().run(ctx)

    assert agent.gojira_app_id == prior_app_id
    assert f"app_status:{prior_app_id}" in gojira.calls


def test_onboard_step_times_out_after_max_polls(session_factory, config):
    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    gojira = FakeGojira(activate_after=999_999)  # never reaches active/failed in time
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    with pytest.raises(RuntimeError, match="timed out"):
        OnboardStep().run(ctx)

    expected_polls = POLL_TIMEOUT_S // POLL_INTERVAL_S
    assert gojira.calls.count("app_status:1") == expected_polls


def test_onboard_step_sleep_called_with_correct_interval(session_factory, config, monkeypatch):
    import pnyx_api.steps as steps_module

    repo = _repo_full_name(config)
    session, agent = _make_agent(session_factory, repo_full_name=repo)
    gojira = FakeGojira(activate_after=3)
    ctx = _make_ctx(session, agent, config, _clients(gojira=gojira))

    # Capture sleep calls
    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(steps_module, "_sleep", fake_sleep)

    OnboardStep().run(ctx)

    # Verify _sleep was called with POLL_INTERVAL_S between polls (but not after the final success)
    assert all(call == POLL_INTERVAL_S for call in sleep_calls)
    # activate_after=3 means 3 polls until success, so 2 sleeps (between polls 1-2 and 2-3)
    assert len(sleep_calls) == 2


# --- AccessStep -----------------------------------------------------------------


def test_access_step_calls_fakes_in_order_with_short_gsa_name(session_factory, config):
    session, agent = _make_agent(session_factory)
    cloud = FakeCloud(project=config.gcp_project)
    ctx = _make_ctx(session, agent, config, _clients(cloud=cloud))

    AccessStep().run(ctx)

    expected_email = f"agent-{AGENT_NAME}@{config.gcp_project}.iam.gserviceaccount.com"
    assert cloud.calls == [
        f"ensure_gsa:agent-{AGENT_NAME}",
        f"bind_workload_identity:{expected_email}:{AGENT_NAME}/agent-{AGENT_NAME}",
        f"add_to_group:{expected_email}:{config.model_group}",
    ]
    assert cloud.gsas == {expected_email}
    assert (expected_email, config.model_group) in cloud.group_members
